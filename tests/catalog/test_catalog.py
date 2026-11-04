from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from typer.testing import CliRunner

from self_service_platform.catalog.catalog import (
    Catalog,
    CatalogError,
    S3BucketOptions,
    SqsQueueOptions,
    build_catalog,
)
from self_service_platform.catalog.component import (
    AwsService,
    Component,
    DeletionBehaviour,
)
from self_service_platform.cli.main import app
from self_service_platform.core.cost import estimate_cost
from self_service_platform.core.manifest import load_manifest_from_string
from self_service_platform.core.validator import validate_manifest

_REQUIRED_TAG_KEYS = frozenset(
    {"tenant_id", "tenant_type", "owner", "managed_by", "component"}
)


class _OptionsWithVersioning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False


def _starters() -> tuple[Component, Component]:
    catalog = build_catalog()
    return catalog.get("s3-bucket"), catalog.get("sqs-queue")


def _version_of(*components: Component) -> str:
    catalog = Catalog()
    for component in components:
        catalog.register(component)
    return catalog.catalog_version


def _with_different_cost(component: Component) -> Component:
    first = component.cost_line_items[0]
    amount = Decimal("1.00") if first.amount != Decimal("1.00") else Decimal("2.00")
    updated = first.model_copy(update={"amount": amount})
    rest = tuple(component.cost_line_items[1:])
    return component.model_copy(update={"cost_line_items": (updated, *rest)})


def _with_extra_permission(component: Component) -> Component:
    action = f"{component.aws_service.value}:CatalogVersionProbe"
    return component.model_copy(
        update={"permissions": (*component.permissions, action)}
    )


def _with_extra_tag(component: Component) -> Component:
    tags = frozenset((*component.required_tags, "cost_center"))
    return component.model_copy(update={"required_tags": tags})


def _with_different_options_schema(component: Component) -> Component:
    return component.model_copy(update={"options_model": _OptionsWithVersioning})


def test_build_catalog_contains_the_starter_components() -> None:
    component_types = {
        component.component_type for component in build_catalog().list_components()
    }

    assert component_types == {
        "lambda-function",
        "log-group",
        "s3-bucket",
        "sqs-queue",
    }


@pytest.mark.parametrize(
    "component",
    build_catalog().list_components(),
    ids=lambda component: component.component_type,
)
def test_every_component_satisfies_catalog_rules(component: Component) -> None:
    assert _REQUIRED_TAG_KEYS <= set(component.required_tags)
    assert component.deletion_behaviour == DeletionBehaviour.DELETE
    assert component.cost_line_items
    assert component.permissions
    assert isinstance(component.aws_service, AwsService)
    prefix = f"{component.aws_service.value}:"
    for action in component.permissions:
        assert action.startswith(prefix)


def test_register_rejects_a_service_outside_the_allowlist() -> None:
    bucket, _queue = _starters()
    values = {name: getattr(bucket, name) for name in type(bucket).model_fields}
    values["component_type"] = "ec2-instance"
    values["aws_service"] = "ec2"
    values["permissions"] = ("ec2:RunInstances",)
    ec2 = Component.model_construct(**values)

    with pytest.raises(CatalogError):
        Catalog().register(ec2)


def test_register_rejects_a_duplicate_component_type() -> None:
    bucket, _queue = _starters()
    catalog = Catalog()
    catalog.register(bucket)

    with pytest.raises(CatalogError):
        catalog.register(bucket.model_copy())


def test_get_unknown_type_raises_catalog_error_naming_the_type() -> None:
    with pytest.raises(CatalogError, match="no-such-component"):
        build_catalog().get("no-such-component")


def test_catalog_version_ignores_registration_order() -> None:
    bucket, queue = _starters()

    assert _version_of(bucket, queue) == _version_of(queue, bucket)


def test_list_components_is_sorted_by_component_type() -> None:
    bucket, queue = _starters()
    catalog = Catalog()
    catalog.register(queue)
    catalog.register(bucket)

    listed = [component.component_type for component in catalog.list_components()]

    assert listed == ["s3-bucket", "sqs-queue"]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_with_different_cost, id="cost-amount"),
        pytest.param(_with_extra_permission, id="permission"),
        pytest.param(_with_extra_tag, id="tag-key"),
        pytest.param(_with_different_options_schema, id="options-schema"),
    ],
)
def test_catalog_version_changes_when_a_definition_changes(
    change: Callable[[Component], Component],
) -> None:
    bucket, queue = _starters()

    assert _version_of(change(bucket), queue) != _version_of(bucket, queue)


@pytest.mark.parametrize("options_model", [S3BucketOptions, SqsQueueOptions])
def test_options_models_reject_unknown_fields(options_model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError):
        options_model.model_validate({"foo": 1})


def test_component_is_frozen() -> None:
    bucket, _queue = _starters()

    with pytest.raises(ValidationError):
        bucket.description = "changed"


def test_register_rejects_an_unknown_replacement_forcing_option() -> None:
    bucket, _queue = _starters()
    component = bucket.model_copy(
        update={"replacement_forcing_options": frozenset({"versioning"})}
    )

    with pytest.raises(CatalogError, match="versioning"):
        Catalog().register(component)


def test_catalog_version_changes_when_replacement_forcing_options_change() -> None:
    bucket, queue = _starters()
    base = bucket.model_copy(update={"options_model": _OptionsWithVersioning})
    flagged = base.model_copy(
        update={"replacement_forcing_options": frozenset({"versioning"})}
    )

    assert _version_of(flagged, queue) != _version_of(base, queue)


def test_starter_components_have_no_replacement_forcing_options() -> None:
    for component in build_catalog().list_components():
        assert component.replacement_forcing_options == frozenset()


def test_catalog_version_changes_when_lambda_function_and_log_group_are_added() -> None:
    catalog = build_catalog()
    bucket = catalog.get("s3-bucket")
    queue = catalog.get("sqs-queue")
    base = _version_of(bucket, queue)
    with_function = _version_of(bucket, queue, catalog.get("lambda-function"))
    with_both = _version_of(
        bucket,
        queue,
        catalog.get("lambda-function"),
        catalog.get("log-group"),
    )

    assert with_function != base
    assert with_both != with_function


@pytest.mark.parametrize("component_type", ["lambda-function", "log-group"])
def test_lambda_function_and_log_group_options_reject_unknown_fields(
    component_type: str,
) -> None:
    options_model = build_catalog().get(component_type).options_model

    with pytest.raises(ValidationError):
        options_model.model_validate({"foo": 1})


_LAMBDA_AND_LOG_GROUP = """\
- type: s3-bucket
  name: uploads
- type: lambda-function
  name: worker
- type: log-group
  name: app
"""

_runner = CliRunner()


def test_manifest_with_lambda_function_and_log_group_validates_and_is_not_priced(
    tmp_path: Path,
) -> None:
    validated = validate_manifest(
        load_manifest_from_string(_LAMBDA_AND_LOG_GROUP),
        build_catalog(),
    )
    estimate = estimate_cost(validated)

    assert [component.component_type for component in estimate.components] == [
        "s3-bucket",
        "lambda-function",
        "log-group",
    ]
    assert estimate.total is None
    for component in estimate.components:
        assert component.total is None
        for item in component.line_items:
            assert item.amount is None
    path = tmp_path / "manifest.yaml"
    path.write_text(_LAMBDA_AND_LOG_GROUP, encoding="utf-8")
    result = _runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    for header in ("uploads s3-bucket", "worker lambda-function", "app log-group"):
        section = lines[lines.index(header) + 1 : lines.index(header) + 3]
        assert any("not priced" in line for line in section)
    assert "manifest total: not priced" in result.stdout

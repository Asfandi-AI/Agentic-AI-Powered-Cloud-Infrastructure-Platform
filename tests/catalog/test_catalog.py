from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

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


def test_build_catalog_contains_the_two_starter_components() -> None:
    component_types = {
        component.component_type for component in build_catalog().list_components()
    }

    assert component_types == {"s3-bucket", "sqs-queue"}


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

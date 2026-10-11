from textwrap import dedent

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.core.manifest import Manifest, load_manifest_from_string
from self_service_platform.core.validator import (
    ManifestValidationError,
    ValidatedEntry,
    ValidatedManifest,
    validate_manifest,
)


class _VersioningOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False


def _load(text: str) -> Manifest:
    return load_manifest_from_string(dedent(text))


def _versioning_component() -> Component:
    return Component(
        component_type="versioned-bucket",
        description="Test component with a versioning option",
        aws_service=AwsService.S3,
        options_model=_VersioningOptions,
        cost_line_items=(CostLineItem(description="storage", amount=None),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=("s3:CreateBucket",),
    )


def _catalog() -> tuple[Catalog, Component]:
    component = _versioning_component()
    catalog = Catalog()
    catalog.register(component)
    return catalog, component


def test_unknown_component_type_names_the_type() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: no-such-type
          name: uploads
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.position == 1
    assert issue.name == "uploads"
    assert "no-such-type" in issue.message


def test_unknown_option_names_the_option() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
          options:
            foo: 1
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.position == 1
    assert issue.name == "uploads"
    assert "foo" in issue.message


def test_wrong_typed_option_names_the_option() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
          options:
            versioning: "maybe"
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.position == 1
    assert issue.name == "uploads"
    assert "versioning" in issue.message


def test_several_problems_are_all_reported() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: no-such-type
          name: uploads
        - type: versioned-bucket
          name: jobs
          options:
            foo: 1
        - type: versioned-bucket
          name: events
          options:
            versioning: "maybe"
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    issues = caught.value.issues
    assert len(issues) == 3
    assert (issues[0].position, issues[0].name) == (1, "uploads")
    assert "no-such-type" in issues[0].message
    assert (issues[1].position, issues[1].name) == (2, "jobs")
    assert "foo" in issues[1].message
    assert (issues[2].position, issues[2].name) == (3, "events")
    assert "versioning" in issues[2].message


def test_issues_are_ordered_by_entry_position() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: no-such-type
          name: first
        - type: versioned-bucket
          name: second
          options:
            versioning: true
        - type: versioned-bucket
          name: third
          options:
            foo: 1
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    issues = caught.value.issues
    assert [issue.position for issue in issues] == [1, 3]
    assert [issue.name for issue in issues] == ["first", "third"]


def test_valid_manifest_returns_validated_manifest() -> None:
    catalog, component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
          options:
            versioning: true
        """
    )

    validated = validate_manifest(manifest, catalog)

    assert isinstance(validated, ValidatedManifest)
    assert validated.catalog_version == catalog.catalog_version
    assert len(validated.entries) == 1
    entry = validated.entries[0]
    assert entry.name == "uploads"
    assert entry.component == component
    assert entry.options == _VersioningOptions(versioning=True)


def test_validating_twice_returns_equal_results() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
          options:
            versioning: true
        """
    )

    assert validate_manifest(manifest, catalog) == validate_manifest(manifest, catalog)


def test_unknown_type_with_options_reports_only_the_unknown_type() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: no-such-type
          name: uploads
          options:
            versioning: true
        """
    )

    with pytest.raises(ManifestValidationError) as caught:
        validate_manifest(manifest, catalog)

    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.position == 1
    assert issue.name == "uploads"
    assert "no-such-type" in issue.message


def test_omitted_options_yield_default_options() -> None:
    catalog, component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
        """
    )

    validated = validate_manifest(manifest, catalog)

    assert len(validated.entries) == 1
    entry = validated.entries[0]
    assert isinstance(entry, ValidatedEntry)
    assert entry.name == "uploads"
    assert entry.component == component
    assert entry.options == _VersioningOptions()


def test_validated_entry_and_manifest_are_frozen() -> None:
    catalog, _component = _catalog()
    manifest = _load(
        """\
        - type: versioned-bucket
          name: uploads
          options:
            versioning: true
        """
    )
    validated = validate_manifest(manifest, catalog)
    entry = validated.entries[0]

    with pytest.raises(ValidationError):
        validated.catalog_version = "changed"
    with pytest.raises(ValidationError):
        entry.name = "changed"

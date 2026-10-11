import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.core.diff import (
    AddedEntry,
    ChangedEntry,
    ManifestDiff,
    RemovedEntry,
    compute_diff,
)
from self_service_platform.core.validator import ValidatedEntry, ValidatedManifest


class _BucketOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False
    encryption: bool = False


def _component(
    component_type: str,
    *,
    aws_service: AwsService = AwsService.S3,
) -> Component:
    return Component(
        component_type=component_type,
        description="Test component",
        aws_service=aws_service,
        options_model=_BucketOptions,
        cost_line_items=(CostLineItem(description="storage", amount=None),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=(f"{aws_service.value}:TestAction",),
        replacement_forcing_options=frozenset({"versioning"}),
    )


def _entry(
    name: str,
    component: Component,
    options: _BucketOptions | None = None,
) -> ValidatedEntry:
    parsed = _BucketOptions() if options is None else options
    return ValidatedEntry(name=name, component=component, options=parsed)


def _manifest(*entries: ValidatedEntry) -> ValidatedManifest:
    return ValidatedManifest(catalog_version="test-catalog", entries=entries)


def test_added_entries_follow_proposed_order() -> None:
    bucket = _component("s3-bucket")
    queue = _component("sqs-queue", aws_service=AwsService.SQS)
    proposed = _manifest(_entry("queue", queue), _entry("bucket", bucket))

    diff = compute_diff(_manifest(), proposed)

    assert diff.added == (
        AddedEntry(name="queue", component_type="sqs-queue", options=_BucketOptions()),
        AddedEntry(name="bucket", component_type="s3-bucket", options=_BucketOptions()),
    )
    assert diff.removed == ()
    assert diff.changed == ()


def test_removed_entries_follow_applied_order() -> None:
    bucket = _component("s3-bucket")
    queue = _component("sqs-queue", aws_service=AwsService.SQS)
    applied_options = _BucketOptions(versioning=True)
    applied = _manifest(
        _entry("queue", queue, applied_options),
        _entry("bucket", bucket),
    )

    diff = compute_diff(applied, _manifest())

    assert diff.removed == (
        RemovedEntry(name="queue", component_type="sqs-queue", options=applied_options),
        RemovedEntry(
            name="bucket",
            component_type="s3-bucket",
            options=_BucketOptions(),
        ),
    )
    assert diff.added == ()
    assert diff.changed == ()


def test_changed_option_is_named() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(_entry("uploads", bucket, _BucketOptions(encryption=True)))

    diff = compute_diff(applied, proposed)

    assert diff.changed == (
        ChangedEntry(
            name="uploads",
            component_type="s3-bucket",
            changed_options=("encryption",),
            replacement_forcing=False,
        ),
    )


def test_changed_entries_follow_proposed_order() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("bucket", bucket), _entry("queue", bucket))
    proposed = _manifest(
        _entry("queue", bucket, _BucketOptions(encryption=True)),
        _entry("bucket", bucket, _BucketOptions(encryption=True)),
    )

    diff = compute_diff(applied, proposed)

    assert [item.name for item in diff.changed] == ["queue", "bucket"]


def test_explicit_default_matches_an_omitted_option() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(
        _entry("uploads", bucket, _BucketOptions(versioning=False, encryption=False))
    )

    diff = compute_diff(applied, proposed)

    assert diff.is_empty is True


def test_component_type_change_is_removed_and_added() -> None:
    applied_options = _BucketOptions(versioning=True)
    proposed_options = _BucketOptions(encryption=True)
    applied = _manifest(_entry("uploads", _component("s3-bucket"), applied_options))
    proposed = _manifest(
        _entry(
            "uploads",
            _component("sqs-queue", aws_service=AwsService.SQS),
            proposed_options,
        )
    )

    diff = compute_diff(applied, proposed)

    assert diff.changed == ()
    assert diff.removed == (
        RemovedEntry(
            name="uploads",
            component_type="s3-bucket",
            options=applied_options,
        ),
    )
    assert diff.added == (
        AddedEntry(
            name="uploads",
            component_type="sqs-queue",
            options=proposed_options,
        ),
    )


def test_replacement_forcing_change_is_a_replacement() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(_entry("uploads", bucket, _BucketOptions(versioning=True)))

    diff = compute_diff(applied, proposed)

    assert diff.changed == (
        ChangedEntry(
            name="uploads",
            component_type="s3-bucket",
            changed_options=("versioning",),
            replacement_forcing=True,
        ),
    )
    assert diff.replacements == diff.changed


def test_non_forcing_option_change_is_not_a_replacement() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(_entry("uploads", bucket, _BucketOptions(encryption=True)))

    diff = compute_diff(applied, proposed)

    assert diff.changed[0].replacement_forcing is False
    assert diff.replacements == ()


def test_forcing_option_among_other_changes_is_a_replacement() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(
        _entry("uploads", bucket, _BucketOptions(encryption=True, versioning=True))
    )

    diff = compute_diff(applied, proposed)

    assert diff.changed == (
        ChangedEntry(
            name="uploads",
            component_type="s3-bucket",
            changed_options=("versioning", "encryption"),
            replacement_forcing=True,
        ),
    )
    assert [item.name for item in diff.replacements] == ["uploads"]


def test_identical_manifests_are_empty() -> None:
    bucket = _component("s3-bucket")
    manifest = _manifest(_entry("uploads", bucket, _BucketOptions(versioning=True)))

    diff = compute_diff(manifest, manifest)

    assert isinstance(diff, ManifestDiff)
    assert diff.is_empty is True
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.changed == ()


def test_entry_order_does_not_change_the_diff() -> None:
    bucket = _component("s3-bucket")
    queue = _component("sqs-queue", aws_service=AwsService.SQS)
    applied = _manifest(_entry("uploads", bucket), _entry("jobs", queue))
    proposed = _manifest(_entry("jobs", queue), _entry("uploads", bucket))

    diff = compute_diff(applied, proposed)

    assert diff.is_empty is True


def test_removals_are_the_removed_entries() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket, _BucketOptions(versioning=True)))

    diff = compute_diff(applied, _manifest())

    assert diff.removals == diff.removed
    assert diff.removals == (
        RemovedEntry(
            name="uploads",
            component_type="s3-bucket",
            options=_BucketOptions(versioning=True),
        ),
    )


def test_each_name_appears_in_exactly_one_group() -> None:
    bucket = _component("s3-bucket")
    queue = _component("sqs-queue", aws_service=AwsService.SQS)
    applied = _manifest(
        _entry("kept", bucket),
        _entry("old", bucket),
        _entry("edited", bucket),
    )
    proposed = _manifest(
        _entry("kept", bucket),
        _entry("edited", bucket, _BucketOptions(encryption=True)),
        _entry("new", queue),
    )

    diff = compute_diff(applied, proposed)
    grouped = [
        item.name
        for group in (diff.added, diff.removed, diff.changed)
        for item in group
    ]

    assert "kept" not in grouped
    assert sorted(grouped) == ["edited", "new", "old"]
    assert len(grouped) == len(set(grouped))


def test_component_type_change_is_a_removal() -> None:
    applied = _manifest(_entry("uploads", _component("s3-bucket")))
    proposed = _manifest(
        _entry("uploads", _component("sqs-queue", aws_service=AwsService.SQS))
    )

    diff = compute_diff(applied, proposed)

    assert [item.name for item in diff.removals] == ["uploads"]
    assert diff.removals[0].component_type == "s3-bucket"
    assert [item.name for item in diff.added] == ["uploads"]
    assert diff.added[0].component_type == "sqs-queue"


def test_unchanged_option_is_omitted_from_changed_options() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket, _BucketOptions(versioning=True)))
    proposed = _manifest(
        _entry("uploads", bucket, _BucketOptions(versioning=True, encryption=True))
    )

    diff = compute_diff(applied, proposed)

    assert diff.changed[0].changed_options == ("encryption",)


def test_diff_models_are_frozen() -> None:
    bucket = _component("s3-bucket")
    queue = _component("sqs-queue", aws_service=AwsService.SQS)
    applied = _manifest(_entry("old", bucket), _entry("edited", bucket))
    proposed = _manifest(
        _entry("edited", bucket, _BucketOptions(encryption=True)),
        _entry("new", queue),
    )
    diff = compute_diff(applied, proposed)

    with pytest.raises(ValidationError):
        diff.added = ()
    with pytest.raises(ValidationError):
        diff.added[0].name = "changed"
    with pytest.raises(ValidationError):
        diff.removed[0].name = "changed"
    with pytest.raises(ValidationError):
        diff.changed[0].name = "changed"


def test_computing_twice_returns_equal_results() -> None:
    bucket = _component("s3-bucket")
    applied = _manifest(_entry("uploads", bucket))
    proposed = _manifest(_entry("uploads", bucket, _BucketOptions(versioning=True)))

    assert compute_diff(applied, proposed) == compute_diff(applied, proposed)

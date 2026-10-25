from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.core.cost import ComponentCost, EstimatedCost, estimate_cost
from self_service_platform.core.validator import ValidatedEntry, ValidatedManifest


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _component(
    component_type: str,
    line_items: tuple[CostLineItem, ...],
    *,
    aws_service: AwsService = AwsService.S3,
) -> Component:
    return Component(
        component_type=component_type,
        description="Test component",
        aws_service=aws_service,
        options_model=_Options,
        cost_line_items=line_items,
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=(f"{aws_service.value}:TestAction",),
    )


def _entry(name: str, component: Component) -> ValidatedEntry:
    return ValidatedEntry(name=name, component=component, options=_Options())


def _manifest(*entries: ValidatedEntry) -> ValidatedManifest:
    return ValidatedManifest(catalog_version="test-catalog", entries=entries)


def test_priced_components_sum_to_the_manifest_total() -> None:
    uploads = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("1.50")),),
    )
    jobs = _component(
        "sqs-queue",
        (CostLineItem(description="requests", amount=Decimal("2.25")),),
        aws_service=AwsService.SQS,
    )

    estimate = estimate_cost(
        _manifest(_entry("uploads", uploads), _entry("jobs", jobs))
    )

    assert isinstance(estimate, EstimatedCost)
    assert isinstance(estimate.components[0], ComponentCost)
    rows = [
        (item.name, item.component_type, item.total) for item in estimate.components
    ]
    assert rows == [
        ("uploads", "s3-bucket", Decimal("1.50")),
        ("jobs", "sqs-queue", Decimal("2.25")),
    ]
    assert estimate.total == Decimal("3.75")


def test_unpriced_line_item_is_not_treated_as_zero() -> None:
    component = _component(
        "s3-bucket",
        (
            CostLineItem(description="storage", amount=Decimal("1.00")),
            CostLineItem(description="requests", amount=None),
        ),
    )

    estimate = estimate_cost(_manifest(_entry("uploads", component)))
    item = estimate.components[0]

    assert [line.amount for line in item.line_items] == [Decimal("1.00"), None]
    assert item.total is None
    assert estimate.total is None


def test_unpriced_component_does_not_count_as_zero() -> None:
    priced = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("1.50")),),
    )
    unpriced = _component(
        "sqs-queue",
        (CostLineItem(description="requests", amount=None),),
        aws_service=AwsService.SQS,
    )

    estimate = estimate_cost(
        _manifest(_entry("uploads", priced), _entry("jobs", unpriced))
    )

    assert estimate.components[0].total == Decimal("1.50")
    assert estimate.components[1].total is None
    assert estimate.total is None


def test_component_with_no_line_items_is_not_priced() -> None:
    unpriced = _component("s3-bucket", ())
    priced = _component(
        "sqs-queue",
        (CostLineItem(description="requests", amount=Decimal("1.50")),),
        aws_service=AwsService.SQS,
    )

    estimate = estimate_cost(
        _manifest(_entry("uploads", unpriced), _entry("jobs", priced))
    )

    assert estimate.components[0].line_items == ()
    assert estimate.components[0].total is None
    assert estimate.components[1].total == Decimal("1.50")
    assert estimate.total is None


def test_zero_amount_is_priced() -> None:
    zero = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal(0)),),
    )
    priced = _component(
        "sqs-queue",
        (CostLineItem(description="requests", amount=Decimal("1.50")),),
        aws_service=AwsService.SQS,
    )

    estimate = estimate_cost(_manifest(_entry("uploads", zero), _entry("jobs", priced)))

    assert estimate.components[0].total is not None
    assert estimate.components[0].total == Decimal(0)
    assert estimate.components[1].total == Decimal("1.50")
    assert estimate.total == Decimal("1.50")


def test_decimal_line_items_sum_exactly() -> None:
    component = _component(
        "s3-bucket",
        (
            CostLineItem(description="storage", amount=Decimal("0.10")),
            CostLineItem(description="requests", amount=Decimal("0.20")),
        ),
    )

    estimate = estimate_cost(_manifest(_entry("uploads", component)))

    assert estimate.components[0].total == Decimal("0.30")
    assert estimate.total == Decimal("0.30")
    assert type(estimate.components[0].total) is Decimal
    assert type(estimate.total) is Decimal


def test_repeated_component_type_is_priced_once_per_entry() -> None:
    component = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("1.00")),),
    )

    estimate = estimate_cost(
        _manifest(_entry("uploads", component), _entry("logs", component))
    )

    assert [item.name for item in estimate.components] == ["uploads", "logs"]
    assert [item.component_type for item in estimate.components] == [
        "s3-bucket",
        "s3-bucket",
    ]
    assert [item.total for item in estimate.components] == [
        Decimal("1.00"),
        Decimal("1.00"),
    ]
    assert estimate.total == Decimal("2.00")


def test_breakdown_order_matches_manifest_order() -> None:
    first = _component(
        "sqs-queue",
        (CostLineItem(description="requests", amount=Decimal("1.00")),),
        aws_service=AwsService.SQS,
    )
    second = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("2.00")),),
    )

    estimate = estimate_cost(
        _manifest(_entry("uploads", first), _entry("jobs", second))
    )

    assert [(item.name, item.component_type) for item in estimate.components] == [
        ("uploads", "sqs-queue"),
        ("jobs", "s3-bucket"),
    ]


def test_estimate_is_labeled_a_poc_heuristic() -> None:
    component = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("1.00")),),
    )

    estimate = estimate_cost(_manifest(_entry("uploads", component)))

    assert estimate.label == "POC heuristic"


def test_estimating_twice_returns_equal_results() -> None:
    component = _component(
        "s3-bucket",
        (CostLineItem(description="storage", amount=Decimal("1.00")),),
    )
    manifest = _manifest(_entry("uploads", component))

    assert estimate_cost(manifest) == estimate_cost(manifest)

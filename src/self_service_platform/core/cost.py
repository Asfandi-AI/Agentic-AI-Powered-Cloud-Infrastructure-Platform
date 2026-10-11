"""Amounts are assumed to share a single unit (the unit is not modelled yet)."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.component import CostLineItem
from self_service_platform.core.validator import ValidatedEntry, ValidatedManifest

_POC_HEURISTIC = "POC heuristic"


class ComponentCost(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    component_type: str
    line_items: tuple[CostLineItem, ...]
    total: Decimal | None


class EstimatedCost(BaseModel):
    model_config = ConfigDict(frozen=True)

    components: tuple[ComponentCost, ...]
    total: Decimal | None
    label: str


def estimate_cost(manifest: ValidatedManifest) -> EstimatedCost:
    components = tuple(_component_cost(entry) for entry in manifest.entries)
    return EstimatedCost(
        components=components,
        total=_manifest_total(components),
        label=_POC_HEURISTIC,
    )


def _component_cost(entry: ValidatedEntry) -> ComponentCost:
    line_items = entry.component.cost_line_items
    return ComponentCost(
        name=entry.name,
        component_type=entry.component.component_type,
        line_items=line_items,
        total=_line_item_total(line_items),
    )


def _line_item_total(line_items: tuple[CostLineItem, ...]) -> Decimal | None:
    if len(line_items) == 0:
        return None
    total = Decimal(0)
    for item in line_items:
        if item.amount is None:
            return None
        total += item.amount
    return total


def _manifest_total(components: tuple[ComponentCost, ...]) -> Decimal | None:
    total = Decimal(0)
    for component in components:
        if component.total is None:
            return None
        total += component.total
    return total

import hashlib
import json
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)

_ALLOWLIST = frozenset(service.value for service in AwsService)

type Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


class CatalogError(Exception):
    """Raised when a Catalog cannot register or find a Component."""


class S3BucketOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SqsQueueOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Catalog:
    def __init__(self) -> None:
        self._components: dict[str, Component] = {}

    def register(self, component: Component) -> None:
        if not _is_allowlisted(component.aws_service):
            raise CatalogError(
                f"service {component.aws_service!r} is outside the Allowlist"
            )
        if component.component_type in self._components:
            raise CatalogError(f"duplicate component_type {component.component_type}")
        _reject_unknown_replacement_options(component)
        self._components[component.component_type] = component

    def get(self, component_type: str) -> Component:
        try:
            return self._components[component_type]
        except KeyError:
            raise CatalogError(f"unknown component type {component_type}") from None

    def list_components(self) -> tuple[Component, ...]:
        return tuple(self._components[key] for key in sorted(self._components))

    @property
    def catalog_version(self) -> str:
        definitions = [_definition(component) for component in self.list_components()]
        encoded = json.dumps(_canonicalize(definitions), sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()


def build_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(_s3_bucket())
    catalog.register(_sqs_queue())
    return catalog


def _s3_bucket() -> Component:
    return Component(
        component_type="s3-bucket",
        description="An S3 bucket",
        aws_service=AwsService.S3,
        options_model=cast(type[BaseModel], S3BucketOptions),
        cost_line_items=(CostLineItem(description="S3 storage", amount=None),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=("s3:CreateBucket", "s3:DeleteBucket"),
    )


def _sqs_queue() -> Component:
    return Component(
        component_type="sqs-queue",
        description="An SQS queue",
        aws_service=AwsService.SQS,
        options_model=cast(type[BaseModel], SqsQueueOptions),
        cost_line_items=(CostLineItem(description="SQS requests", amount=None),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=("sqs:CreateQueue", "sqs:DeleteQueue"),
    )


def _is_allowlisted(service: object) -> bool:
    if isinstance(service, AwsService):
        return True
    return isinstance(service, str) and service in _ALLOWLIST


def _definition(component: Component) -> dict[str, object]:
    return {
        "aws_service": component.aws_service.value,
        "component_type": component.component_type,
        "cost_line_items": [
            {"amount": item.amount, "description": item.description}
            for item in component.cost_line_items
        ],
        "deletion_behaviour": component.deletion_behaviour.value,
        "description": component.description,
        "options_schema": component.options_model.model_json_schema(),
        "permissions": list(component.permissions),
        "replacement_forcing_options": sorted(component.replacement_forcing_options),
        "required_tags": sorted(component.required_tags),
        "untaggable_resource_types": list(component.untaggable_resource_types),
    }


def _reject_unknown_replacement_options(component: Component) -> None:
    fields = set(component.options_model.model_fields)
    unknown = sorted(component.replacement_forcing_options - fields)
    if unknown:
        names = ", ".join(unknown)
        raise CatalogError(f"unknown replacement-forcing option {names}")


def _canonicalize(value: object) -> Json:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")

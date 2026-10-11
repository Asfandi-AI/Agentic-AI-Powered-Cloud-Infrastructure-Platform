from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

REQUIRED_TAG_KEYS = frozenset(
    {"tenant_id", "tenant_type", "owner", "managed_by", "component"}
)


class AwsService(StrEnum):
    S3 = "s3"
    DYNAMODB = "dynamodb"
    SQS = "sqs"
    SNS = "sns"
    LAMBDA = "lambda"
    EVENTBRIDGE = "events"
    HTTP_API = "apigateway"
    LOGS = "logs"


class CostLineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    amount: Decimal | None


class DeletionBehaviour(StrEnum):
    DELETE = "delete"


class Component(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    component_type: str
    description: str
    aws_service: AwsService
    options_model: type[BaseModel]
    cost_line_items: tuple[CostLineItem, ...]
    required_tags: frozenset[str]
    deletion_behaviour: DeletionBehaviour
    permissions: tuple[str, ...]
    untaggable_resource_types: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _reject_missing_tags(self) -> Self:
        missing = REQUIRED_TAG_KEYS - self.required_tags
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"missing required tags: {names}")
        return self

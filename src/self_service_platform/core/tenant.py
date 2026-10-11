from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from self_service_platform.core.manifest import ManifestEntry

ERROR_TEXT_LIMIT = 1024


class TenantStatus(StrEnum):
    CREATING = "creating"
    ACTIVE = "active"
    UPDATING = "updating"
    UPDATE_FAILED = "update_failed"
    DESTROYING = "destroying"


class TenantType(StrEnum):
    DEV = "dev"
    EPHEMERAL = "ephemeral"
    PROD = "prod"


class InitiatorKind(StrEnum):
    CLI = "cli"
    AGENT = "agent"


class DuplicateTenantError(Exception):
    """Raised when a tenant_id is already stored."""


class TenantNotFoundError(Exception):
    """Raised when a tenant_id is not stored."""


class TransitionRejectedError(Exception):
    """Raised when a status change is rejected and nothing is written."""


class Initiator(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: InitiatorKind
    owner: str


class TenantRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    tenant_type: TenantType
    status: TenantStatus
    manifest: tuple[ManifestEntry, ...]
    catalog_version: str
    initiator: Initiator
    expires_at: datetime | None
    last_error: str | None
    updated_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _expires_at_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)

    @field_validator("updated_at")
    @classmethod
    def _updated_at_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class TransitionFields(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: tuple[ManifestEntry, ...]
    catalog_version: str
    initiator: Initiator
    expires_at: datetime | None
    last_error: str | None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)


ALLOWED_TRANSITIONS = frozenset(
    {
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.UPDATING),
        (TenantStatus.UPDATE_FAILED, TenantStatus.UPDATING),
        (TenantStatus.UPDATING, TenantStatus.ACTIVE),
        (TenantStatus.UPDATING, TenantStatus.UPDATE_FAILED),
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        (TenantStatus.UPDATE_FAILED, TenantStatus.DESTROYING),
    }
)


def assert_allowed_transition(expected: TenantStatus, new: TenantStatus) -> None:
    if (expected, new) not in ALLOWED_TRANSITIONS:
        raise TransitionRejectedError(
            f"cannot transition from {expected.value} to {new.value}"
        )


def truncate_error(text: str) -> str:
    return text[:ERROR_TEXT_LIMIT]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must be timezone-aware UTC")
    return value.astimezone(UTC)

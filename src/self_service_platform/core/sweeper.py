from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from self_service_platform.core.registry import Clock, Registry
from self_service_platform.core.service import (
    DestroyFailedError,
    DestroyNotAllowedError,
    PlatformService,
    StalePlanError,
)
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionRejectedError,
    truncate_error,
)

_SERVICE_REQUIRED = "a service is required when dry_run is false"
_NAIVE_CLOCK = "clock must return timezone-aware UTC"
_DRY_RUN = "dry run"
_LIMIT_REACHED = "limit reached"
_SWEEPER = Initiator(kind=InitiatorKind.SWEEPER, owner="sweeper")
_BUSY = frozenset(
    {
        TenantStatus.CREATING,
        TenantStatus.UPDATING,
        TenantStatus.DESTROYING,
    }
)
_ELIGIBLE = frozenset(
    {
        TenantStatus.ACTIVE,
        TenantStatus.UPDATE_FAILED,
        TenantStatus.DESTROY_FAILED,
    }
)


class SweepOutcome(StrEnum):
    DESTROYED = "destroyed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SweepEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    outcome: SweepOutcome
    detail: str


class SweepReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[SweepEntry, ...]


class Sweeper:
    def __init__(
        self,
        registry: Registry,
        clock: Clock,
        service: PlatformService | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._service = service

    def sweep(self, dry_run: bool = True, max_destroy: int = 10) -> SweepReport:
        service = self._service
        if not dry_run and service is None:
            raise ValueError(_SERVICE_REQUIRED)
        now = _utc_instant(self._clock())
        selected = _selected(self._registry.list_tenants(), now)
        if dry_run or service is None:
            return SweepReport(entries=tuple(_preview(selected)))
        return SweepReport(
            entries=tuple(_destroy_selected(service, selected, max_destroy))
        )


def _utc_instant(moment: datetime) -> datetime:
    offset = moment.utcoffset()
    if moment.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError(_NAIVE_CLOCK)
    return moment.astimezone(UTC)


def _selected(
    records: tuple[TenantRecord, ...], now: datetime
) -> tuple[TenantRecord, ...]:
    chosen = [record for record in records if _is_reported(record, now)]
    chosen.sort(key=_by_expiry_then_id)
    return tuple(chosen)


def _is_reported(record: TenantRecord, now: datetime) -> bool:
    if record.tenant_type is TenantType.PROD:
        return False
    expires_at = record.expires_at
    if expires_at is None or expires_at > now:
        return False
    if record.status is TenantStatus.DESTROYED:
        return False
    return record.status in _BUSY or record.status in _ELIGIBLE


def _by_expiry_then_id(record: TenantRecord) -> tuple[datetime, str]:
    expires_at = record.expires_at
    if expires_at is None:
        raise RuntimeError("reported tenant has no expires_at")
    return (expires_at, record.tenant_id)


def _preview(records: tuple[TenantRecord, ...]) -> tuple[SweepEntry, ...]:
    return tuple(_skipped(record, _preview_detail(record)) for record in records)


def _preview_detail(record: TenantRecord) -> str:
    if record.status in _BUSY:
        return record.status.value
    return _DRY_RUN


def _destroy_selected(
    service: PlatformService,
    records: tuple[TenantRecord, ...],
    max_destroy: int,
) -> tuple[SweepEntry, ...]:
    entries: list[SweepEntry] = []
    destroyed = 0
    for record in records:
        if record.status in _BUSY:
            entries.append(_skipped(record, record.status.value))
            continue
        if destroyed >= max_destroy:
            entries.append(_skipped(record, _LIMIT_REACHED))
            continue
        entry = _destroy_one(service, record)
        entries.append(entry)
        if entry.outcome is SweepOutcome.DESTROYED:
            destroyed += 1
    return tuple(entries)


def _destroy_one(service: PlatformService, record: TenantRecord) -> SweepEntry:
    try:
        plan = service.plan_destroy(record.tenant_id)
        # The TTL was shown and accepted at create time. --yes is that confirmation.
        token = service.confirm_destroy(plan, _SWEEPER)
        service.apply_destroy(plan, confirmed=True, initiator=_SWEEPER, token=token)
    except (DestroyNotAllowedError, StalePlanError, TransitionRejectedError) as exc:
        return _skipped(record, truncate_error(str(exc)))
    except DestroyFailedError as exc:
        return SweepEntry(
            tenant_id=record.tenant_id,
            outcome=SweepOutcome.FAILED,
            detail=truncate_error(str(exc)),
        )
    return SweepEntry(
        tenant_id=record.tenant_id,
        outcome=SweepOutcome.DESTROYED,
        detail="",
    )


def _skipped(record: TenantRecord, detail: str) -> SweepEntry:
    return SweepEntry(
        tenant_id=record.tenant_id,
        outcome=SweepOutcome.SKIPPED,
        detail=detail,
    )

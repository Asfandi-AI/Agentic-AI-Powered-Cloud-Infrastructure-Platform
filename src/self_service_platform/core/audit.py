"""Audit entries. Nothing in the CLI or the service calls this module yet."""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from self_service_platform.core.registry import Clock
from self_service_platform.core.tenant import Initiator, InitiatorKind

_CLOCK_MUST_BE_UTC = "clock must return a UTC datetime"
_CATALOG_VERSION = re.compile(r"[0-9a-f]{64}")
_COMPONENT_TYPE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_IDENTITY_LIMIT = 128
_ARGUMENT_NAMES = (
    "catalog_version",
    "component_type",
    "data_loss_confirmed",
    "dry_run",
    "manifest_entry_count",
    "removal_count",
    "replacement_count",
)
_COUNT_NAMES = frozenset({"manifest_entry_count", "removal_count", "replacement_count"})
_BOOL_NAMES = frozenset({"data_loss_confirmed", "dry_run"})


class ToolName(StrEnum):
    LIST_CATALOG = "list_catalog"
    DRAFT_MANIFEST = "draft_manifest"
    VALIDATE_MANIFEST = "validate_manifest"
    ESTIMATE = "estimate"
    PLAN_CREATE = "plan_create"
    PLAN_UPDATE = "plan_update"
    EXECUTE_CREATE = "execute_create"
    EXECUTE_UPDATE = "execute_update"
    LIST_TENANTS = "list_tenants"
    TENANT_STATUS = "tenant_status"
    DESTROY = "destroy"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"


class Decision(StrEnum):
    CONFIRMED = "confirmed"
    DECLINED = "declined"


_DECISION_TOOLS = frozenset(
    {ToolName.EXECUTE_CREATE, ToolName.EXECUTE_UPDATE, ToolName.DESTROY}
)


class InvalidAuditArgumentError(Exception):
    """Raised when an audit field or argument is not allowed."""


class AuditLogError(Exception):
    """Raised when one or more audit sinks fail. The message is only the count."""

    def __init__(self, failed: int) -> None:
        if failed == 1:
            super().__init__("1 audit sink failed")
            return
        super().__init__(f"{failed} audit sinks failed")


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    time: datetime
    initiator_kind: InitiatorKind
    initiator_owner: str
    tool_name: ToolName
    tenant_id: str | None
    arguments: dict[str, Any]
    outcome: Outcome | None
    decision: Decision | None


class AuditLog(Protocol):
    def write(self, entry: AuditEntry) -> None:
        """Append one entry."""


class CompositeAuditLog:
    """Writes one entry to every sink, then reports how many failed."""

    def __init__(self, sinks: Sequence[AuditLog]) -> None:
        self._sinks = tuple(sinks)

    def write(self, entry: AuditEntry) -> None:
        failed = 0
        for sink in self._sinks:
            try:
                sink.write(entry)
            except Exception:  # noqa: BLE001 - the report is only the failure count
                failed += 1
        if failed == 0:
            return
        raise AuditLogError(failed)


def record_tool_call(
    log: AuditLog,
    clock: Clock,
    *,
    initiator: Initiator,
    tool: ToolName,
    tenant_id: str | None,
    arguments: Mapping[str, object],
    outcome: Outcome,
) -> None:
    now = _utc_instant(clock())
    _require_tool(tool)
    _require_outcome(outcome)
    owner = _identity(initiator.owner)
    tenant = _tenant_id(tenant_id)
    checked = _tool_arguments(arguments)
    log.write(
        AuditEntry(
            time=now,
            initiator_kind=initiator.kind,
            initiator_owner=owner,
            tool_name=tool,
            tenant_id=tenant,
            arguments=checked,
            outcome=outcome,
            decision=None,
        )
    )


def record_plan_decision(
    log: AuditLog,
    clock: Clock,
    *,
    initiator: Initiator,
    tool: ToolName,
    tenant_id: str | None,
    decision: Decision,
    arguments: Mapping[str, object] | None = None,
) -> None:
    now = _utc_instant(clock())
    _require_decision_tool(tool)
    _require_decision(decision)
    owner = _identity(initiator.owner)
    tenant = _tenant_id(tenant_id)
    checked = _decision_arguments(arguments)
    log.write(
        AuditEntry(
            time=now,
            initiator_kind=initiator.kind,
            initiator_owner=owner,
            tool_name=tool,
            tenant_id=tenant,
            arguments=checked,
            outcome=None,
            decision=decision,
        )
    )


def _utc_instant(moment: datetime) -> datetime:
    offset = moment.utcoffset()
    if moment.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError(_CLOCK_MUST_BE_UTC)
    return moment.astimezone(UTC)


def _require_tool(tool: ToolName) -> None:
    if not isinstance(tool, ToolName):
        raise InvalidAuditArgumentError("tool name is not allowed")


def _require_outcome(outcome: Outcome) -> None:
    if not isinstance(outcome, Outcome):
        raise InvalidAuditArgumentError("outcome is not allowed")


def _require_decision(decision: Decision) -> None:
    if not isinstance(decision, Decision):
        raise InvalidAuditArgumentError("decision is not allowed")


def _require_decision_tool(tool: ToolName) -> None:
    if tool not in _DECISION_TOOLS:
        raise InvalidAuditArgumentError("that tool has no plan decision")


def _identity(value: object) -> str:
    if not isinstance(value, str) or len(value) > _IDENTITY_LIMIT or _control(value):
        raise InvalidAuditArgumentError("identity is not allowed")
    return value


def _tenant_id(value: object) -> str | None:
    if value is None:
        return None
    return _identity(value)


def _control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _tool_arguments(values: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise InvalidAuditArgumentError("arguments must be a mapping")
    unknown = set(values) - set(_ARGUMENT_NAMES)
    if unknown:
        raise InvalidAuditArgumentError("unknown audit argument")
    checked: dict[str, Any] = {name: None for name in _ARGUMENT_NAMES}
    for name in _ARGUMENT_NAMES:
        if name not in values:
            continue
        checked[name] = _argument(name, values[name])
    return checked


def _decision_arguments(values: Mapping[str, object] | None) -> dict[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or len(values) > 0:
        raise InvalidAuditArgumentError("a decision has no arguments")
    return {}


def _argument(name: str, value: object) -> Any:
    if value is None:
        return None
    if name in _COUNT_NAMES:
        return _count(value)
    if name in _BOOL_NAMES:
        return _flag(value)
    if name == "catalog_version":
        return _catalog_version(value)
    return _component_type(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidAuditArgumentError("count must be a non-negative int")
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise InvalidAuditArgumentError("flag must be a bool")
    return value


def _catalog_version(value: object) -> str:
    if not isinstance(value, str) or _CATALOG_VERSION.fullmatch(value) is None:
        raise InvalidAuditArgumentError("catalog_version is not allowed")
    return value


def _component_type(value: object) -> str:
    if not isinstance(value, str) or _COMPONENT_TYPE.fullmatch(value) is None:
        raise InvalidAuditArgumentError("component_type is not allowed")
    return value

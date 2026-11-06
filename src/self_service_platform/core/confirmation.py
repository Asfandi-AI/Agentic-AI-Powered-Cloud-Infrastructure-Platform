"""Single-use confirmation tokens. This module does not issue them."""

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from self_service_platform.core.cost import EstimatedCost
from self_service_platform.core.diff import AddedEntry, ManifestDiff, RemovedEntry
from self_service_platform.core.registry import Clock
from self_service_platform.core.service import CreatePlan, DestroyPlan, UpdatePlan
from self_service_platform.core.validator import ValidatedManifest

_CLOCK_MUST_BE_UTC = "clock must return a UTC datetime"
_PLAN_DATETIME_MUST_BE_AWARE = "plan datetime must be an aware instant"


class OperationKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"


class TokenAlreadyUsedError(Exception):
    """Raised when a confirmation token is consumed a second time."""


class TokenExpiredError(Exception):
    """Raised when a confirmation token is consumed at or after its expiry."""


class PlanMismatchError(Exception):
    """Raised when a token is presented for a different plan or operation."""


class UnknownTokenError(Exception):
    """Raised when a token is not in the store."""


class DataLossNotConfirmedError(Exception):
    """Raised when a removal or destroy is issued without the data-loss flag."""


class ReplacementConfirmationError(Exception):
    """Raised when the confirmed replacement names differ from the plan."""


class NothingToConfirmError(Exception):
    """Raised when an update changes nothing."""


class _TokenRecord:
    def __init__(self, digest: str, expires_at: datetime) -> None:
        self.digest = digest
        self.expires_at = expires_at
        self.used = False


class ConfirmationStore:
    """In-memory tokens for one process. Issue and consume share its lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _TokenRecord] = {}

    def put(self, token: str, digest: str, expires_at: datetime, now: datetime) -> None:
        with self._lock:
            self._purge(now)
            self._tokens[token] = _TokenRecord(digest, expires_at)

    def consume(self, token: str, digest: str, now: datetime) -> None:
        with self._lock:
            record = self._tokens.get(token)
            if record is None:
                raise UnknownTokenError("unknown confirmation token")
            if now >= record.expires_at:
                del self._tokens[token]
                raise TokenExpiredError("confirmation token has expired")
            if record.digest != digest:
                raise PlanMismatchError("confirmation token is for a different plan")
            if record.used:
                raise TokenAlreadyUsedError("confirmation token is already used")
            record.used = True

    def _purge(self, now: datetime) -> None:
        expired = [
            token for token, record in self._tokens.items() if now >= record.expires_at
        ]
        for token in expired:
            del self._tokens[token]


class ConfirmationVerifier:
    """Checks a confirmation token. It has no way to issue one."""

    def __init__(self, store: ConfirmationStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def consume_create(self, token: str, plan: CreatePlan) -> None:
        self._consume(token, OperationKind.CREATE, plan)

    def consume_update(self, token: str, plan: UpdatePlan) -> None:
        self._consume(token, OperationKind.UPDATE, plan)

    def consume_destroy(self, token: str, plan: DestroyPlan) -> None:
        self._consume(token, OperationKind.DESTROY, plan)

    def _consume(
        self,
        token: str,
        operation: OperationKind,
        plan: CreatePlan | UpdatePlan | DestroyPlan,
    ) -> None:
        now = _utc_instant(self._clock())
        self._store.consume(token, plan_digest(operation, plan), now)


def plan_digest(
    operation: OperationKind,
    plan: CreatePlan | UpdatePlan | DestroyPlan,
) -> str:
    """SHA-256 of the operation and the plan fields, in canonical JSON."""
    encoded = json.dumps(
        _payload(operation, plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _utc_instant(moment: datetime) -> datetime:
    offset = moment.utcoffset()
    if moment.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError(_CLOCK_MUST_BE_UTC)
    return moment.astimezone(UTC)


def _payload(
    operation: OperationKind,
    plan: CreatePlan | UpdatePlan | DestroyPlan,
) -> dict[str, object]:
    if isinstance(plan, CreatePlan):
        return {
            "catalog_version": plan.catalog_version,
            "estimated_cost": _cost(plan.estimated_cost),
            "expires_at": _instant(plan.expires_at),
            "manifest": _entries(plan.manifest),
            "operation": operation.value,
            "owner": plan.owner,
            "tenant_id": plan.tenant_id,
            "tenant_type": plan.tenant_type.value,
        }
    if isinstance(plan, UpdatePlan):
        return {
            "base_manifest": _entries(plan.base_manifest),
            "catalog_version": plan.catalog_version,
            "diff": _diff(plan.diff),
            "estimated_cost": _cost(plan.estimated_cost),
            "new_manifest": _entries(plan.new_manifest),
            "nothing_to_change": plan.nothing_to_change,
            "operation": operation.value,
            "removals": list(plan.removals),
            "replacements": list(plan.replacements),
            "tenant_id": plan.tenant_id,
        }
    return {
        "component_names": list(plan.component_names),
        "operation": operation.value,
        "statement": plan.statement,
        "status": plan.status.value,
        "tenant_id": plan.tenant_id,
        "tenant_type": plan.tenant_type.value,
    }


def _entries(manifest: ValidatedManifest) -> list[dict[str, object]]:
    return [
        {
            "component_type": entry.component.component_type,
            "name": entry.name,
            "options": _canonicalize(entry.options.model_dump(mode="json")),
        }
        for entry in manifest.entries
    ]


def _diff(diff: ManifestDiff) -> dict[str, object]:
    return {
        "added": [_changed_view(entry) for entry in diff.added],
        "changed": [
            {
                "changed_options": list(entry.changed_options),
                "component_type": entry.component_type,
                "name": entry.name,
                "replacement_forcing": entry.replacement_forcing,
            }
            for entry in diff.changed
        ],
        "removed": [_changed_view(entry) for entry in diff.removed],
    }


def _changed_view(entry: AddedEntry | RemovedEntry) -> dict[str, object]:
    return {
        "component_type": entry.component_type,
        "name": entry.name,
        "options": _canonicalize(entry.options.model_dump(mode="json")),
    }


def _cost(cost: EstimatedCost) -> dict[str, object]:
    return {
        "components": [
            {
                "component_type": component.component_type,
                "line_items": [
                    {
                        "amount": _amount(item.amount),
                        "description": item.description,
                    }
                    for item in component.line_items
                ],
                "name": component.name,
                "total": _amount(component.total),
            }
            for component in cost.components
        ],
        "label": cost.label,
        "total": _amount(cost.total),
    }


def _amount(amount: Decimal | None) -> str | None:
    if amount is None:
        return None
    return str(amount)


def _instant(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    offset = moment.utcoffset()
    if moment.tzinfo is None or offset is None:
        raise ValueError(_PLAN_DATETIME_MUST_BE_AWARE)
    return moment.astimezone(UTC).isoformat()


def _canonicalize(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")

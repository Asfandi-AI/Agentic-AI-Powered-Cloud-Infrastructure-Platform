"""Issues confirmation tokens. The verifier module does not import this module."""

import secrets
from datetime import datetime, timedelta

from self_service_platform.core.confirmation import (
    ConfirmationStore,
    ConfirmationVerifier,
    DataLossNotConfirmedError,
    NothingToConfirmError,
    OperationKind,
    ReplacementConfirmationError,
    _utc_instant,
    plan_digest,
)
from self_service_platform.core.registry import Clock
from self_service_platform.core.service import CreatePlan, DestroyPlan, UpdatePlan

# POC value, not from the spec.
_POC_TTL = timedelta(minutes=15)


class ConfirmationIssuer:
    """Issues one single-use token for one exact plan."""

    def __init__(self, store: ConfirmationStore, clock: Clock, ttl: timedelta) -> None:
        self._store = store
        self._clock = clock
        self._ttl = ttl

    def issue_create(self, plan: CreatePlan) -> str:
        return self._save(OperationKind.CREATE, plan, _utc_instant(self._clock()))

    def issue_update(
        self,
        plan: UpdatePlan,
        *,
        data_loss_confirmed: bool,
        confirmed_replacements: frozenset[str],
    ) -> str:
        _refuse_update(plan, data_loss_confirmed, confirmed_replacements)
        return self._save(OperationKind.UPDATE, plan, _utc_instant(self._clock()))

    def issue_destroy(self, plan: DestroyPlan, *, data_loss_confirmed: bool) -> str:
        if not data_loss_confirmed:
            raise DataLossNotConfirmedError("destroy requires data-loss confirmation")
        return self._save(OperationKind.DESTROY, plan, _utc_instant(self._clock()))

    def _save(
        self,
        operation: OperationKind,
        plan: CreatePlan | UpdatePlan | DestroyPlan,
        now: datetime,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._store.put(token, plan_digest(operation, plan), now + self._ttl, now)
        return token


def confirmation_pair(
    clock: Clock,
    ttl: timedelta = _POC_TTL,
) -> tuple[ConfirmationIssuer, ConfirmationVerifier]:
    """Return an issuer and a verifier that share one in-memory store."""
    store = ConfirmationStore()
    return (
        ConfirmationIssuer(store, clock, ttl),
        ConfirmationVerifier(store, clock),
    )


def _refuse_update(
    plan: UpdatePlan,
    data_loss_confirmed: bool,
    confirmed_replacements: frozenset[str],
) -> None:
    if plan.nothing_to_change:
        raise NothingToConfirmError("an empty update has nothing to confirm")
    if len(plan.removals) > 0 and not data_loss_confirmed:
        raise DataLossNotConfirmedError("removals require data-loss confirmation")
    if confirmed_replacements != frozenset(plan.replacements):
        raise ReplacementConfirmationError(
            "confirmed replacements must equal the plan replacements"
        )

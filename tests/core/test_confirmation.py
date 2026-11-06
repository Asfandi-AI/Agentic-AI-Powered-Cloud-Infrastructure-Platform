import queue
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.core.confirmation import (
    ConfirmationVerifier,
    DataLossNotConfirmedError,
    NothingToConfirmError,
    OperationKind,
    PlanMismatchError,
    ReplacementConfirmationError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    UnknownTokenError,
    plan_digest,
)
from self_service_platform.core.confirmation_issuer import (
    ConfirmationIssuer,
    confirmation_pair,
)
from self_service_platform.core.cost import estimate_cost
from self_service_platform.core.diff import compute_diff
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.service import CreatePlan, DestroyPlan, UpdatePlan
from self_service_platform.core.tenant import TenantStatus, TenantType
from self_service_platform.core.validator import ValidatedManifest, validate_manifest

_TTL = timedelta(minutes=15)
_START = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


def _clock() -> _Clock:
    return _Clock(_START)


def _pair(clock: _Clock) -> tuple[ConfirmationIssuer, ConfirmationVerifier]:
    return confirmation_pair(clock, ttl=_TTL)


def _catalog(*, replacement: bool) -> Catalog:
    forcing = frozenset({"enabled"}) if replacement else frozenset()
    catalog = Catalog()
    catalog.register(
        Component(
            component_type="box",
            description="A box",
            aws_service=AwsService.S3,
            options_model=_Options,
            cost_line_items=(CostLineItem(description="storage", amount=None),),
            required_tags=REQUIRED_TAG_KEYS,
            deletion_behaviour=DeletionBehaviour.DELETE,
            permissions=("s3:TestAction",),
            replacement_forcing_options=forcing,
        )
    )
    return catalog


def _validated(
    catalog: Catalog,
    entries: list[dict[str, object]],
) -> ValidatedManifest:
    return validate_manifest(Manifest.model_validate(entries), catalog)


def _box(name: str, *, enabled: bool) -> dict[str, object]:
    return {"type": "box", "name": name, "options": {"enabled": enabled}}


def _create_plan(tenant_id: str = "tenant-a") -> CreatePlan:
    catalog = _catalog(replacement=False)
    manifest = _validated(catalog, [_box("box", enabled=False)])
    return CreatePlan(
        tenant_id=tenant_id,
        tenant_type=TenantType.DEV,
        owner="owner-a",
        manifest=manifest,
        catalog_version=manifest.catalog_version,
        estimated_cost=estimate_cost(manifest),
        expires_at=_START + timedelta(days=1),
    )


def _update_plan(
    new_entries: list[dict[str, object]],
    base_entries: list[dict[str, object]] | None = None,
    *,
    replacement: bool = False,
) -> UpdatePlan:
    catalog = _catalog(replacement=replacement)
    base = _validated(catalog, base_entries or [_box("box", enabled=False)])
    proposed = _validated(catalog, new_entries)
    diff = compute_diff(applied=base, proposed=proposed)
    return UpdatePlan(
        tenant_id="tenant-a",
        base_manifest=base,
        new_manifest=proposed,
        catalog_version=catalog.catalog_version,
        diff=diff,
        estimated_cost=estimate_cost(proposed),
        nothing_to_change=diff.is_empty,
        removals=tuple(entry.name for entry in diff.removed),
        replacements=tuple(entry.name for entry in diff.replacements),
    )


def _destroy_plan() -> DestroyPlan:
    return DestroyPlan(
        tenant_id="tenant-a",
        tenant_type=TenantType.DEV,
        component_names=("box",),
        status=TenantStatus.ACTIVE,
        statement="all resources and data of the tenant will be deleted",
    )


def test_the_same_plan_has_one_digest() -> None:
    plan = _create_plan()

    assert plan_digest(OperationKind.CREATE, plan) == plan_digest(
        OperationKind.CREATE, plan
    )


def test_digest_changes_when_the_tenant_id_changes() -> None:
    plan = _create_plan()
    other = plan.model_copy(update={"tenant_id": "tenant-b"})

    assert plan_digest(OperationKind.CREATE, plan) != plan_digest(
        OperationKind.CREATE, other
    )


def test_digest_changes_when_the_operation_kind_changes() -> None:
    plan = _create_plan()

    assert plan_digest(OperationKind.CREATE, plan) != plan_digest(
        OperationKind.DESTROY, plan
    )


def test_consume_accepts_the_issued_token_once() -> None:
    issuer, verifier = _pair(_clock())
    plan = _create_plan()
    token = issuer.issue_create(plan)

    verifier.consume_create(token, plan)


def test_consume_accepts_a_destroy_token_when_data_loss_is_confirmed() -> None:
    issuer, verifier = _pair(_clock())
    plan = _destroy_plan()
    token = issuer.issue_destroy(plan, data_loss_confirmed=True)

    verifier.consume_destroy(token, plan)


def test_consume_accepts_an_update_with_confirmed_removals() -> None:
    issuer, verifier = _pair(_clock())
    plan = _update_plan(
        [_box("box", enabled=False)],
        [_box("box", enabled=False), _box("other", enabled=False)],
    )
    token = issuer.issue_update(
        plan,
        data_loss_confirmed=True,
        confirmed_replacements=frozenset(plan.replacements),
    )

    verifier.consume_update(token, plan)


def test_consume_accepts_an_update_with_confirmed_replacements() -> None:
    issuer, verifier = _pair(_clock())
    plan = _update_plan([_box("box", enabled=True)], replacement=True)
    token = issuer.issue_update(
        plan,
        data_loss_confirmed=False,
        confirmed_replacements=frozenset(plan.replacements),
    )

    verifier.consume_update(token, plan)


def test_second_consume_raises_token_already_used() -> None:
    issuer, verifier = _pair(_clock())
    plan = _create_plan()
    token = issuer.issue_create(plan)
    verifier.consume_create(token, plan)

    with pytest.raises(TokenAlreadyUsedError):
        verifier.consume_create(token, plan)


def test_consume_just_before_expiry_succeeds() -> None:
    clock = _clock()
    issuer, verifier = _pair(clock)
    plan = _create_plan()
    token = issuer.issue_create(plan)
    clock.now = _START + _TTL - timedelta(microseconds=1)

    verifier.consume_create(token, plan)


def test_consume_at_expiry_raises_token_expired() -> None:
    clock = _clock()
    issuer, verifier = _pair(clock)
    plan = _create_plan()
    token = issuer.issue_create(plan)
    clock.now = _START + _TTL

    with pytest.raises(TokenExpiredError):
        verifier.consume_create(token, plan)


def test_an_expired_token_is_unknown_on_the_next_consume() -> None:
    clock = _clock()
    issuer, verifier = _pair(clock)
    plan = _create_plan()
    token = issuer.issue_create(plan)
    clock.now = _START + _TTL
    with pytest.raises(TokenExpiredError):
        verifier.consume_create(token, plan)

    with pytest.raises(UnknownTokenError):
        verifier.consume_create(token, plan)


def test_a_different_plan_raises_plan_mismatch_and_can_still_be_consumed() -> None:
    issuer, verifier = _pair(_clock())
    plan = _create_plan()
    other = plan.model_copy(update={"owner": "owner-b"})
    token = issuer.issue_create(plan)

    with pytest.raises(PlanMismatchError):
        verifier.consume_create(token, other)
    verifier.consume_create(token, plan)


def test_another_operation_raises_plan_mismatch_and_can_still_be_consumed() -> None:
    issuer, verifier = _pair(_clock())
    plan = _create_plan()
    token = issuer.issue_create(plan)

    with pytest.raises(PlanMismatchError):
        verifier.consume_destroy(token, _destroy_plan())
    verifier.consume_create(token, plan)


def test_an_unknown_token_raises_unknown_token() -> None:
    _issuer, verifier = _pair(_clock())

    with pytest.raises(UnknownTokenError):
        verifier.consume_create("missing-token", _create_plan())


def test_a_changed_token_string_raises_unknown_token() -> None:
    issuer, verifier = _pair(_clock())
    plan = _create_plan()
    token = issuer.issue_create(plan)

    with pytest.raises(UnknownTokenError):
        verifier.consume_create(f"{token}x", plan)


def test_destroy_without_the_data_loss_flag_is_refused() -> None:
    issuer, _verifier = _pair(_clock())

    with pytest.raises(DataLossNotConfirmedError):
        issuer.issue_destroy(_destroy_plan(), data_loss_confirmed=False)


def test_update_removals_without_the_data_loss_flag_is_refused() -> None:
    issuer, _verifier = _pair(_clock())
    plan = _update_plan(
        [_box("box", enabled=False)],
        [_box("box", enabled=False), _box("other", enabled=False)],
    )

    with pytest.raises(DataLossNotConfirmedError):
        issuer.issue_update(
            plan,
            data_loss_confirmed=False,
            confirmed_replacements=frozenset(),
        )


def test_update_replacements_must_name_every_replacement() -> None:
    issuer, _verifier = _pair(_clock())
    plan = _update_plan(
        [_box("box", enabled=True)],
        replacement=True,
    )

    with pytest.raises(ReplacementConfirmationError):
        issuer.issue_update(
            plan,
            data_loss_confirmed=False,
            confirmed_replacements=frozenset(),
        )


def test_update_with_an_extra_confirmed_replacement_is_refused() -> None:
    issuer, _verifier = _pair(_clock())
    plan = _update_plan([_box("box", enabled=True)], replacement=True)
    confirmed = frozenset(plan.replacements) | {"not-in-the-plan"}

    with pytest.raises(ReplacementConfirmationError):
        issuer.issue_update(
            plan,
            data_loss_confirmed=False,
            confirmed_replacements=confirmed,
        )


def test_an_empty_update_is_refused() -> None:
    issuer, _verifier = _pair(_clock())
    plan = _update_plan([_box("box", enabled=False)])

    with pytest.raises(NothingToConfirmError):
        issuer.issue_update(
            plan,
            data_loss_confirmed=False,
            confirmed_replacements=frozenset(),
        )


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 6, 10, 0),
        datetime(2026, 11, 6, 10, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_a_clock_that_is_not_utc_is_rejected(moment: datetime) -> None:
    clock = _Clock(moment)
    issuer, _verifier = confirmation_pair(clock, ttl=_TTL)

    with pytest.raises(ValueError):
        issuer.issue_create(_create_plan())


def test_the_issuer_and_the_verifier_are_different_objects() -> None:
    issuer, verifier = _pair(_clock())

    assert issuer is not verifier


def test_the_verifier_has_no_issue_methods() -> None:
    _issuer, verifier = _pair(_clock())

    assert all(not name.startswith("issue") for name in dir(verifier))


def _changed_create(plan: CreatePlan, field: str) -> CreatePlan:
    other = _validated(_catalog(replacement=False), [_box("other", enabled=True)])
    values: dict[str, object] = {
        "tenant_id": "tenant-b",
        "tenant_type": TenantType.PROD,
        "owner": "owner-b",
        "manifest": other,
        "catalog_version": f"{plan.catalog_version}x",
        "estimated_cost": plan.estimated_cost.model_copy(update={"label": "other"}),
        "expires_at": None,
    }
    return plan.model_copy(update={field: values[field]})


def _changed_update(plan: UpdatePlan, field: str) -> UpdatePlan:
    catalog = _catalog(replacement=False)
    other = _validated(catalog, [_box("other", enabled=False)])
    same = _validated(catalog, [_box("box", enabled=False)])
    values: dict[str, object] = {
        "tenant_id": "tenant-b",
        "base_manifest": other,
        "new_manifest": other,
        "catalog_version": f"{plan.catalog_version}x",
        "diff": compute_diff(applied=same, proposed=same),
        "estimated_cost": plan.estimated_cost.model_copy(update={"label": "other"}),
        "nothing_to_change": not plan.nothing_to_change,
        "removals": ("box",),
        "replacements": ("box",),
    }
    return plan.model_copy(update={field: values[field]})


def _changed_destroy(plan: DestroyPlan, field: str) -> DestroyPlan:
    values: dict[str, object] = {
        "tenant_id": "tenant-b",
        "tenant_type": TenantType.PROD,
        "component_names": ("other",),
        "status": TenantStatus.UPDATE_FAILED,
        "statement": "other",
    }
    return plan.model_copy(update={field: values[field]})


@pytest.mark.parametrize("field", list(CreatePlan.model_fields))
def test_changing_one_create_plan_field_raises_plan_mismatch(field: str) -> None:
    plan = _create_plan()
    changed = _changed_create(plan, field)
    issuer, verifier = _pair(_clock())
    token = issuer.issue_create(plan)

    with pytest.raises(PlanMismatchError):
        verifier.consume_create(token, changed)


@pytest.mark.parametrize("field", list(UpdatePlan.model_fields))
def test_changing_one_update_plan_field_raises_plan_mismatch(field: str) -> None:
    plan = _update_plan([_box("box", enabled=True)])
    changed = _changed_update(plan, field)
    issuer, verifier = _pair(_clock())
    token = issuer.issue_update(
        plan,
        data_loss_confirmed=False,
        confirmed_replacements=frozenset(),
    )

    with pytest.raises(PlanMismatchError):
        verifier.consume_update(token, changed)


@pytest.mark.parametrize("field", list(DestroyPlan.model_fields))
def test_changing_one_destroy_plan_field_raises_plan_mismatch(field: str) -> None:
    plan = _destroy_plan()
    changed = _changed_destroy(plan, field)
    issuer, verifier = _pair(_clock())
    token = issuer.issue_destroy(plan, data_loss_confirmed=True)

    with pytest.raises(PlanMismatchError):
        verifier.consume_destroy(token, changed)


def _consume_once(
    barrier: threading.Barrier,
    verifier: ConfirmationVerifier,
    token: str,
    plan: CreatePlan,
    outcomes: queue.Queue[str],
) -> None:
    barrier.wait()
    try:
        verifier.consume_create(token, plan)
    except TokenAlreadyUsedError:
        outcomes.put("used")
    else:
        outcomes.put("ok")


def test_two_threads_consuming_the_same_token_let_exactly_one_succeed() -> None:
    plan = _create_plan()
    # Stress check, not proof of the lock.
    for _round in range(200):
        issuer, verifier = _pair(_clock())
        token = issuer.issue_create(plan)
        outcomes: queue.Queue[str] = queue.Queue()
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(
                target=_consume_once,
                args=(barrier, verifier, token, plan, outcomes),
            )
            for _each in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes.get(timeout=5) for _each in range(2)) == ["ok", "used"]


def test_two_issued_tokens_are_different() -> None:
    issuer, _verifier = _pair(_clock())
    plan = _create_plan()

    assert issuer.issue_create(plan) != issuer.issue_create(plan)


def test_expired_entries_are_removed_when_a_new_token_is_issued() -> None:
    clock = _clock()
    issuer, verifier = _pair(clock)
    plan = _create_plan()
    expired = issuer.issue_create(plan)
    clock.now = _START + _TTL
    issuer.issue_create(plan)

    with pytest.raises(UnknownTokenError):
        verifier.consume_create(expired, plan)

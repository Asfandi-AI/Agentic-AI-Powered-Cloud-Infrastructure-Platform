from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.core.cost import estimate_cost
from self_service_platform.core.diff import compute_diff
from self_service_platform.core.executor import (
    Executor,
    ExecutorError,
    ProvisionRequest,
    ResourceRef,
)
from self_service_platform.core.manifest import Manifest, ManifestEntry
from self_service_platform.core.registry import Registry
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    CreateResult,
    PlatformService,
    ProvisionFailedError,
    StalePlanError,
    StoredManifestInvalidError,
    TtlPolicy,
    UpdateConfirmations,
    UpdateFailedError,
    UpdateNotAllowedError,
    UpdatePlan,
    UpdateResult,
)
from self_service_platform.core.tags import MANAGED_BY
from self_service_platform.core.tenant import (
    ERROR_TEXT_LIMIT,
    DuplicateTenantError,
    Initiator,
    InitiatorKind,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
    truncate_error,
)
from self_service_platform.core.validator import (
    ManifestValidationError,
    validate_manifest,
)
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
TENANT_ID = "tenant-a"
DEV_TTL = timedelta(days=1)
EPHEMERAL_TTL = timedelta(hours=3)
_LONG_ERROR = "e" * (ERROR_TEXT_LIMIT + 10)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingRegistry:
    def __init__(self, inner: SqliteRegistry, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def create_tenant(self, record: TenantRecord) -> None:
        self._calls.append("create_tenant")
        self._inner.create_tenant(record)

    def get(self, tenant_id: str) -> TenantRecord:
        return self._inner.get(tenant_id)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        self._calls.append("transition")
        return self._inner.transition(tenant_id, expected, new, fields)


class RecordingExecutor:
    def __init__(
        self,
        inner: FakeExecutor,
        registry: SqliteRegistry,
        calls: list[str],
    ) -> None:
        self._inner = inner
        self._registry = registry
        self._calls = calls
        self.status_during_provision: TenantStatus | None = None

    def provision(self, request: ProvisionRequest) -> None:
        self._calls.append("provision")
        self.status_during_provision = self._registry.get(request.tenant_id).status
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self._calls.append("destroy")
        self._inner.destroy(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return self._inner.tenant_role_exists(tenant_id)


class LongProvisionError(FakeExecutor):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls
        self.fail_next_provision(after=1)

    def provision(self, request: ProvisionRequest) -> None:
        self._calls.append("provision")
        try:
            super().provision(request)
        except ExecutorError:
            raise ExecutorError(_LONG_ERROR) from None

    def destroy(self, tenant_id: str) -> None:
        self._calls.append("destroy")
        super().destroy(tenant_id)


class KeyErrorExecutor:
    def __init__(self) -> None:
        self.error = KeyError("boom")
        self.destroy_calls: list[str] = []

    def provision(self, request: ProvisionRequest) -> None:
        raise self.error

    def destroy(self, tenant_id: str) -> None:
        self.destroy_calls.append(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        return ()

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return False


class ActivationFails:
    def __init__(self, inner: SqliteRegistry) -> None:
        self._inner = inner
        self.error = RuntimeError("active write failed")

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def get(self, tenant_id: str) -> TenantRecord:
        return self._inner.get(tenant_id)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        if expected is TenantStatus.CREATING and new is TenantStatus.ACTIVE:
            raise self.error
        return self._inner.transition(tenant_id, expected, new, fields)


@dataclass
class Bundle:
    service: PlatformService
    catalog: Catalog
    sqlite: SqliteRegistry
    calls: list[str]
    clock: MutableClock
    executor: RecordingExecutor


def _policy() -> TtlPolicy:
    return TtlPolicy(dev=DEV_TTL, ephemeral=EPHEMERAL_TTL)


def _manifest(*entries: tuple[str, str]) -> Manifest:
    if not entries:
        entries = (("s3-bucket", "uploads"), ("sqs-queue", "jobs"))
    return Manifest.model_validate(
        [{"type": component_type, "name": name} for component_type, name in entries]
    )


def _initiator() -> Initiator:
    return Initiator(kind=InitiatorKind.CLI, owner="bob")


def _bundle(sqlite: SqliteRegistry, clock: MutableClock) -> Bundle:
    calls: list[str] = []
    catalog = build_catalog()
    fake = FakeExecutor()
    executor = RecordingExecutor(fake, sqlite, calls)
    registry = RecordingRegistry(sqlite, calls)
    service = PlatformService(
        catalog=catalog,
        registry=registry,
        executor=executor,
        clock=clock,
        ttl_policy=_policy(),
    )
    return Bundle(
        service=service,
        catalog=catalog,
        sqlite=sqlite,
        calls=calls,
        clock=clock,
        executor=executor,
    )


def _assert_absent(sqlite: SqliteRegistry) -> None:
    with pytest.raises(TenantNotFoundError):
        sqlite.get(TENANT_ID)


def test_rejected_manifest_has_no_side_effects(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        manifest = Manifest.model_validate(
            [{"type": "no-such-type", "name": "uploads"}]
        )

        with pytest.raises(ManifestValidationError):
            bundle.service.plan_create(TENANT_ID, TenantType.DEV, "alice", manifest)

        assert bundle.calls == []
        _assert_absent(sqlite)


def test_missing_confirmation_has_no_side_effects(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )

        with pytest.raises(ConfirmationRequiredError):
            bundle.service.apply_create(plan, False, _initiator())

        assert bundle.calls == []
        _assert_absent(sqlite)


@pytest.mark.parametrize("confirmed", ["yes", 1])
def test_truthy_non_bool_confirmation_is_refused(
    tmp_path: Path,
    confirmed: object,
) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )

        with pytest.raises(ConfirmationRequiredError):
            bundle.service.apply_create(plan, confirmed, _initiator())

        assert bundle.calls == []
        _assert_absent(sqlite)


def test_provision_runs_while_status_is_creating(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )

        bundle.service.apply_create(plan, True, _initiator())

        assert bundle.executor.status_during_provision is TenantStatus.CREATING
        assert bundle.calls == ["create_tenant", "provision", "transition"]


def test_create_ends_active_with_manifest_version_initiator_and_tags(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )

        result = bundle.service.apply_create(plan, True, _initiator())

        assert isinstance(result, CreateResult)
        record = result.record
        assert record.status is TenantStatus.ACTIVE
        assert record.manifest == (
            ManifestEntry(type="s3-bucket", name="uploads", options={}),
            ManifestEntry(type="sqs-queue", name="jobs", options={}),
        )
        assert record.catalog_version == bundle.catalog.catalog_version
        assert record.initiator == _initiator()
        resources = bundle.executor.list_tagged_resources(TENANT_ID)
        assert [resource.tags["component"] for resource in resources] == [
            "jobs",
            "uploads",
        ]
        for resource in resources:
            assert dict(resource.tags) == {
                "tenant_id": TENANT_ID,
                "tenant_type": TenantType.DEV.value,
                "owner": "alice",
                "managed_by": MANAGED_BY,
                "component": resource.tags["component"],
            }


def test_plan_shows_components_cost_and_expires_at(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        manifest = _manifest()

        dev = bundle.service.plan_create(TENANT_ID, TenantType.DEV, "alice", manifest)
        prod = bundle.service.plan_create(TENANT_ID, TenantType.PROD, "alice", manifest)
        ephemeral = bundle.service.plan_create(
            TENANT_ID, TenantType.EPHEMERAL, "alice", manifest
        )

        assert [
            (entry.name, entry.component.component_type)
            for entry in dev.manifest.entries
        ] == [
            ("uploads", "s3-bucket"),
            ("jobs", "sqs-queue"),
        ]
        assert dev.estimated_cost == estimate_cost(dev.manifest)
        assert dev.estimated_cost.total is None
        assert dev.estimated_cost.label == "POC heuristic"
        assert dev.expires_at == T0 + DEV_TTL
        assert prod.expires_at is None
        assert ephemeral.expires_at == T0 + EPHEMERAL_TTL


def test_plan_create_has_no_side_effects(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)

        bundle.service.plan_create(TENANT_ID, TenantType.DEV, "alice", _manifest())

        assert bundle.calls == []
        _assert_absent(sqlite)


def test_duplicate_tenant_id_is_rejected_before_provision(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        bundle.service.apply_create(plan, True, _initiator())
        bundle.calls.clear()

        with pytest.raises(DuplicateTenantError):
            bundle.service.apply_create(plan, True, _initiator())

        assert bundle.calls == ["create_tenant"]


def test_failed_provision_leaves_creating_and_keeps_partial_resources(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        executor = LongProvisionError(calls)
        service = PlatformService(
            catalog=build_catalog(),
            registry=sqlite,
            executor=executor,
            clock=clock,
            ttl_policy=_policy(),
        )
        plan = service.plan_create(TENANT_ID, TenantType.DEV, "alice", _manifest())

        with pytest.raises(ProvisionFailedError) as caught:
            service.apply_create(plan, True, _initiator())

        cause = caught.value.__cause__
        assert isinstance(cause, ExecutorError)
        assert str(caught.value) == truncate_error(str(cause))
        assert len(str(cause)) > ERROR_TEXT_LIMIT
        assert sqlite.get(TENANT_ID).status is TenantStatus.CREATING
        assert "destroy" not in calls
        resources = executor.list_tagged_resources(TENANT_ID)
        assert [resource.tags["component"] for resource in resources] == ["uploads"]


def test_stale_catalog_version_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        stale = plan.model_copy(update={"catalog_version": "stale"})

        with pytest.raises(StalePlanError):
            bundle.service.apply_create(stale, True, _initiator())

        assert bundle.calls == []
        _assert_absent(sqlite)


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
def test_expired_plan_is_refused(tmp_path: Path, offset: timedelta) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        expired = plan.model_copy(update={"expires_at": clock.now + offset})

        with pytest.raises(StalePlanError):
            bundle.service.apply_create(expired, True, _initiator())

        assert bundle.calls == []
        _assert_absent(sqlite)


def test_stored_expires_at_is_the_plan_value_after_the_clock_moves(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        clock.now = T0 + timedelta(hours=1)

        result = bundle.service.apply_create(plan, True, _initiator())

        assert result.record.expires_at == plan.expires_at
        assert result.record.expires_at == T0 + DEV_TTL


def test_non_executor_error_propagates_and_leaves_creating(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    executor = KeyErrorExecutor()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        service = PlatformService(
            catalog=build_catalog(),
            registry=sqlite,
            executor=executor,
            clock=clock,
            ttl_policy=_policy(),
        )
        plan = service.plan_create(TENANT_ID, TenantType.DEV, "alice", _manifest())

        with pytest.raises(KeyError) as caught:
            service.apply_create(plan, True, _initiator())

        assert caught.value is executor.error
        assert sqlite.get(TENANT_ID).status is TenantStatus.CREATING
        assert executor.destroy_calls == []


def test_failed_activation_leaves_creating_and_keeps_resources(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        registry = ActivationFails(sqlite)
        executor = RecordingExecutor(FakeExecutor(), sqlite, calls)
        service = PlatformService(
            catalog=build_catalog(),
            registry=registry,
            executor=executor,
            clock=clock,
            ttl_policy=_policy(),
        )
        plan = service.plan_create(TENANT_ID, TenantType.DEV, "alice", _manifest())

        with pytest.raises(RuntimeError) as caught:
            service.apply_create(plan, True, _initiator())

        assert caught.value is registry.error
        assert sqlite.get(TENANT_ID).status is TenantStatus.CREATING
        assert "destroy" not in calls
        resources = executor.list_tagged_resources(TENANT_ID)
        assert [resource.tags["component"] for resource in resources] == [
            "jobs",
            "uploads",
        ]


def test_rejected_duplicate_leaves_the_first_tenant_untouched(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        first = bundle.service.apply_create(plan, True, _initiator())
        resources = bundle.executor.list_tagged_resources(TENANT_ID)

        with pytest.raises(DuplicateTenantError):
            bundle.service.apply_create(plan, True, _initiator())

        record = sqlite.get(TENANT_ID)
        assert record.status is TenantStatus.ACTIVE
        assert record.manifest == first.record.manifest
        assert record.catalog_version == first.record.catalog_version
        assert record.initiator == first.record.initiator
        assert record.expires_at == first.record.expires_at
        assert record == first.record
        assert bundle.executor.list_tagged_resources(TENANT_ID) == resources


def test_prod_plan_applies_and_stores_no_expires_at(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.PROD, "alice", _manifest()
        )

        result = bundle.service.apply_create(plan, True, _initiator())

        assert result.record.status is TenantStatus.ACTIVE
        assert result.record.expires_at is None
        assert sqlite.get(TENANT_ID).expires_at is None


class _BucketOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False


class _QueueOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


_TO_STATUS: dict[TenantStatus, tuple[tuple[TenantStatus, TenantStatus], ...]] = {
    TenantStatus.CREATING: (),
    TenantStatus.ACTIVE: ((TenantStatus.CREATING, TenantStatus.ACTIVE),),
    TenantStatus.UPDATING: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.UPDATING),
    ),
    TenantStatus.UPDATE_FAILED: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.UPDATING),
        (TenantStatus.UPDATING, TenantStatus.UPDATE_FAILED),
    ),
    TenantStatus.DESTROYING: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
    ),
}


def _replacement_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(
        Component(
            component_type="versioned-bucket",
            description="A bucket",
            aws_service=AwsService.S3,
            options_model=_BucketOptions,
            cost_line_items=(CostLineItem(description="storage", amount=None),),
            required_tags=REQUIRED_TAG_KEYS,
            deletion_behaviour=DeletionBehaviour.DELETE,
            permissions=("s3:TestAction",),
            replacement_forcing_options=frozenset({"versioning"}),
        )
    )
    catalog.register(
        Component(
            component_type="sqs-queue",
            description="A queue",
            aws_service=AwsService.SQS,
            options_model=_QueueOptions,
            cost_line_items=(CostLineItem(description="requests", amount=None),),
            required_tags=REQUIRED_TAG_KEYS,
            deletion_behaviour=DeletionBehaviour.DELETE,
            permissions=("sqs:TestAction",),
        )
    )
    return catalog


def _seed(
    sqlite: SqliteRegistry,
    *,
    status: TenantStatus,
    manifest: tuple[ManifestEntry, ...],
    catalog_version: str,
    owner: str = "alice",
) -> TenantRecord:
    sqlite.create_tenant(
        TenantRecord(
            tenant_id=TENANT_ID,
            tenant_type=TenantType.DEV,
            status=TenantStatus.CREATING,
            manifest=manifest,
            catalog_version=catalog_version,
            owner=owner,
            initiator=_initiator(),
            expires_at=T0 + DEV_TTL,
            last_error=None,
            updated_at=T0,
        )
    )
    current = sqlite.get(TENANT_ID)
    for expected, new in _TO_STATUS[status]:
        last_error = "provision failed" if new == TenantStatus.UPDATE_FAILED else None
        sqlite.transition(
            TENANT_ID,
            expected,
            new,
            TransitionFields(
                manifest=current.manifest,
                catalog_version=current.catalog_version,
                initiator=current.initiator,
                expires_at=current.expires_at,
                last_error=last_error,
            ),
        )
    return sqlite.get(TENANT_ID)


def _update_service(
    sqlite: SqliteRegistry,
    clock: MutableClock,
    catalog: Catalog,
    calls: list[str],
) -> PlatformService:
    return PlatformService(
        catalog=catalog,
        registry=RecordingRegistry(sqlite, calls),
        executor=RecordingExecutor(FakeExecutor(), sqlite, calls),
        clock=clock,
        ttl_policy=_policy(),
    )


def test_get_returns_the_plan_owner_and_the_initiator_owner(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        bundle = _bundle(sqlite, clock)
        plan = bundle.service.plan_create(
            TENANT_ID, TenantType.DEV, "alice", _manifest()
        )
        bundle.service.apply_create(plan, True, _initiator())

        record = sqlite.get(TENANT_ID)

        assert record.owner == "alice"
        assert record.initiator.owner == "bob"


def test_plan_update_unknown_tenant_raises_tenant_not_found(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        service = _update_service(sqlite, clock, build_catalog(), calls)

        with pytest.raises(TenantNotFoundError):
            service.plan_update("missing", _manifest())

        assert calls == []


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(TenantStatus.CREATING, id="creating"),
        pytest.param(TenantStatus.UPDATING, id="updating"),
        pytest.param(TenantStatus.DESTROYING, id="destroying"),
    ],
)
def test_plan_update_rejects_a_status_that_is_not_updatable(
    tmp_path: Path,
    status: TenantStatus,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=status,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)

        with pytest.raises(UpdateNotAllowedError) as caught:
            service.plan_update(TENANT_ID, _manifest())

        assert status.value in str(caught.value)
        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_plan_update_allows_update_failed(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.UPDATE_FAILED,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])

        plan = service.plan_update(TENANT_ID, _manifest())

        assert isinstance(plan, UpdatePlan)
        assert plan.tenant_id == TENANT_ID


def test_plan_update_rejects_an_invalid_new_manifest(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        proposed = Manifest.model_validate(
            [{"type": "no-such-type", "name": "uploads"}]
        )

        with pytest.raises(ManifestValidationError):
            service.plan_update(TENANT_ID, proposed)

        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_plan_update_reports_diff_cost_removals_and_replacements(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    stored_manifest = (
        ManifestEntry(
            type="versioned-bucket",
            name="uploads",
            options={"versioning": False},
        ),
        ManifestEntry(type="sqs-queue", name="jobs", options={}),
    )
    proposed = Manifest.model_validate(
        [
            {
                "type": "versioned-bucket",
                "name": "uploads",
                "options": {"versioning": True},
            },
            {"type": "versioned-bucket", "name": "reports"},
        ]
    )
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_manifest,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])

        plan = service.plan_update(TENANT_ID, proposed)

        assert [entry.name for entry in plan.base_manifest.entries] == [
            "uploads",
            "jobs",
        ]
        assert [entry.name for entry in plan.new_manifest.entries] == [
            "uploads",
            "reports",
        ]
        assert plan.catalog_version == catalog.catalog_version
        assert plan.nothing_to_change is False
        assert plan.diff == compute_diff(
            applied=plan.base_manifest,
            proposed=plan.new_manifest,
        )
        assert plan.estimated_cost == estimate_cost(plan.new_manifest)
        assert plan.removals == ("jobs",)
        assert plan.replacements == ("uploads",)


def test_plan_update_has_no_side_effects(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    proposed = _manifest(
        ("s3-bucket", "uploads"),
        ("sqs-queue", "jobs"),
        ("sqs-queue", "reports"),
    )
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)

        service.plan_update(TENANT_ID, proposed)

        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_plan_update_rejects_a_stored_manifest_invalid_for_the_catalog(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    stored_manifest = (ManifestEntry(type="retired", name="uploads", options={}),)
    proposed = _manifest(("s3-bucket", "uploads"))
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_manifest,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)

        with pytest.raises(StoredManifestInvalidError) as caught:
            service.plan_update(TENANT_ID, proposed)

        with pytest.raises(ManifestValidationError) as expected:
            validate_manifest(
                Manifest.model_validate([{"type": "retired", "name": "uploads"}]),
                catalog,
            )
        assert caught.value.issues == expected.value.issues
        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_reordered_manifest_is_nothing_to_change(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    proposed = _manifest(("sqs-queue", "jobs"), ("s3-bucket", "uploads"))
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])

        plan = service.plan_update(TENANT_ID, proposed)

        assert plan.nothing_to_change is True
        assert plan.removals == ()
        assert plan.replacements == ()


def test_update_plan_matches_the_stored_and_proposed_manifests(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    stored = _manifest()
    proposed = _manifest(("s3-bucket", "uploads"), ("sqs-queue", "reports"))
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(stored.root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])

        plan = service.plan_update(TENANT_ID, proposed)

        assert plan.tenant_id == TENANT_ID
        assert plan.catalog_version == catalog.catalog_version
        assert plan.base_manifest == validate_manifest(stored, catalog)
        assert plan.new_manifest == validate_manifest(proposed, catalog)
        with pytest.raises(ValidationError):
            plan.tenant_id = "other"


def test_type_change_is_a_removal_and_an_addition(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    stored = (ManifestEntry(type="s3-bucket", name="uploads", options={}),)
    proposed = _manifest(("sqs-queue", "uploads"))
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])

        plan = service.plan_update(TENANT_ID, proposed)

        assert "uploads" in plan.removals
        assert "uploads" in [entry.name for entry in plan.diff.added]
        assert "uploads" not in plan.replacements


def _updater() -> Initiator:
    return Initiator(kind=InitiatorKind.AGENT, owner="carol")


def _confirmed(*names: str) -> UpdateConfirmations:
    return UpdateConfirmations(
        update=True,
        removals=True,
        replacements=frozenset(names),
    )


def _added_manifest() -> Manifest:
    return _manifest(
        ("s3-bucket", "uploads"),
        ("sqs-queue", "jobs"),
        ("sqs-queue", "reports"),
    )


def _removed_manifest() -> Manifest:
    return _manifest(("s3-bucket", "uploads"))


def _type_change_manifest() -> Manifest:
    return _manifest(("sqs-queue", "uploads"))


_VERSIONED = (
    ManifestEntry(
        type="versioned-bucket",
        name="uploads",
        options={"versioning": False},
    ),
    ManifestEntry(type="sqs-queue", name="jobs", options={}),
)


def _replaced_manifest(*, keep_jobs: bool) -> Manifest:
    entries: list[dict[str, object]] = [
        {
            "type": "versioned-bucket",
            "name": "uploads",
            "options": {"versioning": True},
        }
    ]
    if keep_jobs:
        entries.append({"type": "sqs-queue", "name": "jobs"})
    return Manifest.model_validate(entries)


class _OrderedExecutor(RecordingExecutor):
    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        self._calls.append("list_tagged_resources")
        return super().list_tagged_resources(tenant_id, component)


class _ReportsLeftover(FakeExecutor):
    def __init__(self, calls: list[str], component: str) -> None:
        super().__init__()
        self._calls = calls
        self._component = component

    def provision(self, request: ProvisionRequest) -> None:
        self._calls.append("provision")
        super().provision(request)

    def destroy(self, tenant_id: str) -> None:
        self._calls.append("destroy")
        super().destroy(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        self._calls.append("list_tagged_resources")
        found = super().list_tagged_resources(tenant_id, component)
        if component != self._component:
            return found
        leftover = ResourceRef(
            resource_type=AwsService.S3.value,
            identifier=f"{tenant_id}/{component}-old",
            tags={"component": component},
        )
        return (*found, leftover)


class _ProvisionExplodes(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.error = ExecutorError(_LONG_ERROR)
        self.destroy_calls: list[str] = []

    def provision(self, request: ProvisionRequest) -> None:
        raise self.error

    def destroy(self, tenant_id: str) -> None:
        self.destroy_calls.append(tenant_id)


class _KeyProvision:
    def __init__(self) -> None:
        self.error = KeyError("boom")
        self.destroy_calls: list[str] = []

    def provision(self, request: ProvisionRequest) -> None:
        raise self.error

    def destroy(self, tenant_id: str) -> None:
        self.destroy_calls.append(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        return ()

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return False


class _TransitionFails:
    def __init__(self, inner: SqliteRegistry, new: TenantStatus) -> None:
        self._inner = inner
        self._new = new
        self.error = RuntimeError(f"{new.value} write failed")

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def get(self, tenant_id: str) -> TenantRecord:
        return self._inner.get(tenant_id)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        if new is self._new and expected is TenantStatus.UPDATING:
            raise self.error
        return self._inner.transition(tenant_id, expected, new, fields)


def _service(
    catalog: Catalog,
    registry: Registry,
    executor: Executor,
    clock: MutableClock,
) -> PlatformService:
    return PlatformService(
        catalog=catalog,
        registry=registry,
        executor=executor,
        clock=clock,
        ttl_policy=_policy(),
    )


def _shift(
    sqlite: SqliteRegistry,
    manifest: tuple[ManifestEntry, ...],
    status: TenantStatus,
) -> None:
    current = sqlite.get(TENANT_ID)
    steps = {
        TenantStatus.UPDATING: ((TenantStatus.ACTIVE, TenantStatus.UPDATING),),
        TenantStatus.ACTIVE: (
            (TenantStatus.ACTIVE, TenantStatus.UPDATING),
            (TenantStatus.UPDATING, TenantStatus.ACTIVE),
        ),
    }
    for expected, new in steps[status]:
        sqlite.transition(
            TENANT_ID,
            expected,
            new,
            TransitionFields(
                manifest=manifest,
                catalog_version=current.catalog_version,
                initiator=current.initiator,
                expires_at=current.expires_at,
                last_error=None,
            ),
        )


def test_empty_diff_applies_with_no_confirmation_or_side_effects(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _manifest())
        refused = UpdateConfirmations(
            update=False,
            removals=False,
            replacements=frozenset(),
        )

        result = service.apply_update(plan, refused, _updater())

        assert result == UpdateResult(changed=False, record=stored)
        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_missing_update_confirmation_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _added_manifest())
        refused = UpdateConfirmations(
            update=False,
            removals=True,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "update" in str(caught.value)


@pytest.mark.parametrize("update", ["yes", 1])
def test_truthy_non_bool_update_confirmation_is_refused(
    tmp_path: Path,
    update: object,
) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _added_manifest())
        refused = UpdateConfirmations.model_construct(
            update=update,
            removals=True,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "update" in str(caught.value)


def test_removals_need_their_own_confirmation(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _removed_manifest())
        refused = UpdateConfirmations(
            update=True,
            removals=False,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "removals" in str(caught.value)


def test_missing_replacement_name_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=_VERSIONED,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _replaced_manifest(keep_jobs=True))
        refused = UpdateConfirmations(
            update=True,
            removals=True,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "uploads" in str(caught.value)


def test_wrong_replacement_name_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=_VERSIONED,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _replaced_manifest(keep_jobs=True))
        refused = UpdateConfirmations(
            update=True,
            removals=True,
            replacements=frozenset({"spare"}),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "uploads" in str(caught.value)


def test_confirmed_replacement_proceeds(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=_VERSIONED,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _replaced_manifest(keep_jobs=True))

        result = service.apply_update(plan, _confirmed("uploads"), _updater())

        uploads = next(
            entry for entry in result.record.manifest if entry.name == "uploads"
        )
        assert result.record.status is TenantStatus.ACTIVE
        assert uploads.options["versioning"] is True


def test_extra_replacement_names_are_ignored(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=_VERSIONED,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _replaced_manifest(keep_jobs=True))
        confirmations = _confirmed("uploads", "spare")

        result = service.apply_update(plan, confirmations, _updater())

        assert result.changed is True
        assert result.record.status is TenantStatus.ACTIVE


def test_refused_applies_leave_the_registry_and_executor_untouched(
    tmp_path: Path,
) -> None:
    def untouched(
        name: str,
        catalog: Catalog,
        manifest: tuple[ManifestEntry, ...],
        proposed: Manifest,
        confirmations: object,
        *,
        rewrite: tuple[tuple[ManifestEntry, ...], TenantStatus] | None = None,
        stale_version: bool = False,
    ) -> None:
        clock = MutableClock(T0)
        calls: list[str] = []
        with SqliteRegistry(tmp_path / name, clock) as sqlite:
            stored = _seed(
                sqlite,
                status=TenantStatus.ACTIVE,
                manifest=manifest,
                catalog_version=catalog.catalog_version,
            )
            service = _update_service(sqlite, clock, catalog, calls)
            plan = service.plan_update(TENANT_ID, proposed)
            if rewrite is not None:
                _shift(sqlite, rewrite[0], rewrite[1])
                stored = sqlite.get(TENANT_ID)
            if stale_version:
                plan = plan.model_copy(update={"catalog_version": "stale"})
            with pytest.raises(
                (ConfirmationRequiredError, StalePlanError, UpdateNotAllowedError)
            ):
                service.apply_update(plan, confirmations, _updater())
            assert calls == []
            assert sqlite.get(TENANT_ID) == stored

    standard = build_catalog()
    stored_entries = tuple(_manifest().root)
    untouched(
        "missing-update.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        UpdateConfirmations(update=False, removals=True, replacements=frozenset()),
    )
    untouched(
        "yes.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        UpdateConfirmations.model_construct(
            update="yes",
            removals=True,
            replacements=frozenset(),
        ),
    )
    untouched(
        "one.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        UpdateConfirmations.model_construct(
            update=1,
            removals=True,
            replacements=frozenset(),
        ),
    )
    untouched(
        "missing-removals.sqlite",
        standard,
        stored_entries,
        _removed_manifest(),
        UpdateConfirmations(update=True, removals=False, replacements=frozenset()),
    )
    versioned = _replacement_catalog()
    untouched(
        "missing-replacement.sqlite",
        versioned,
        _VERSIONED,
        _replaced_manifest(keep_jobs=True),
        UpdateConfirmations(update=True, removals=True, replacements=frozenset()),
    )
    untouched(
        "wrong-replacement.sqlite",
        versioned,
        _VERSIONED,
        _replaced_manifest(keep_jobs=True),
        UpdateConfirmations(
            update=True,
            removals=True,
            replacements=frozenset({"spare"}),
        ),
    )
    untouched(
        "stale-manifest.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        _confirmed(),
        rewrite=(tuple(_removed_manifest().root), TenantStatus.ACTIVE),
    )
    untouched(
        "stale-version.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        _confirmed(),
        stale_version=True,
    )
    untouched(
        "now-updating.sqlite",
        standard,
        stored_entries,
        _added_manifest(),
        _confirmed(),
        rewrite=(stored_entries, TenantStatus.UPDATING),
    )


def test_stale_base_manifest_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _added_manifest())
        _shift(sqlite, tuple(_removed_manifest().root), TenantStatus.ACTIVE)

        with pytest.raises(StalePlanError):
            service.apply_update(plan, _confirmed(), _updater())

        assert calls == []


def test_stale_update_catalog_version_is_refused(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _added_manifest())
        stale = plan.model_copy(update={"catalog_version": "stale"})

        with pytest.raises(StalePlanError):
            service.apply_update(stale, _confirmed(), _updater())

        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_tenant_that_became_updating_is_rejected_without_provision(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    stored_entries = tuple(_manifest().root)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_entries,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _added_manifest())
        _shift(sqlite, stored_entries, TenantStatus.UPDATING)

        with pytest.raises(UpdateNotAllowedError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        assert "updating" in str(caught.value)
        assert "provision" not in calls
        assert sqlite.get(TENANT_ID).status is TenantStatus.UPDATING


def test_provision_runs_while_status_is_updating(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        executor = _OrderedExecutor(FakeExecutor(), sqlite, calls)
        service = _service(
            catalog,
            RecordingRegistry(sqlite, calls),
            executor,
            clock,
        )
        plan = service.plan_update(TENANT_ID, _removed_manifest())

        service.apply_update(plan, _confirmed(), _updater())

        assert executor.status_during_provision is TenantStatus.UPDATING
        assert calls == [
            "transition",
            "provision",
            "list_tagged_resources",
            "transition",
        ]


def test_add_stores_manifest_version_initiator_owner_and_expiry(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
            owner="alice",
        )
        executor = RecordingExecutor(FakeExecutor(), sqlite, calls)
        service = _service(
            catalog,
            RecordingRegistry(sqlite, calls),
            executor,
            clock,
        )
        plan = service.plan_update(TENANT_ID, _added_manifest())

        result = service.apply_update(plan, _confirmed(), _updater())

        record = sqlite.get(TENANT_ID)
        assert result == UpdateResult(changed=True, record=record)
        assert record.status is TenantStatus.ACTIVE
        assert record.manifest == (
            ManifestEntry(type="s3-bucket", name="uploads", options={}),
            ManifestEntry(type="sqs-queue", name="jobs", options={}),
            ManifestEntry(type="sqs-queue", name="reports", options={}),
        )
        assert record.catalog_version == catalog.catalog_version
        assert record.initiator == _updater()
        assert record.expires_at == stored.expires_at
        assert record.owner == "alice"
        resources = executor.list_tagged_resources(TENANT_ID, component="reports")
        assert len(resources) == 1
        assert dict(resources[0].tags) == {
            "tenant_id": TENANT_ID,
            "tenant_type": TenantType.DEV.value,
            "owner": "alice",
            "managed_by": MANAGED_BY,
            "component": "reports",
        }


def test_removed_component_resources_are_gone(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        executor = RecordingExecutor(FakeExecutor(), sqlite, calls)
        service = _service(
            catalog,
            RecordingRegistry(sqlite, calls),
            executor,
            clock,
        )
        plan = service.plan_update(TENANT_ID, _removed_manifest())

        result = service.apply_update(plan, _confirmed(), _updater())

        assert result.record.status is TenantStatus.ACTIVE
        assert executor.list_tagged_resources(TENANT_ID, component="jobs") == ()


def test_type_change_passes_verification(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    stored_manifest = (ManifestEntry(type="s3-bucket", name="uploads", options={}),)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_manifest,
            catalog_version=catalog.catalog_version,
        )
        executor = RecordingExecutor(FakeExecutor(), sqlite, calls)
        service = _service(
            catalog,
            RecordingRegistry(sqlite, calls),
            executor,
            clock,
        )
        plan = service.plan_update(TENANT_ID, _type_change_manifest())

        result = service.apply_update(plan, _confirmed(), _updater())

        resources = executor.list_tagged_resources(TENANT_ID, component="uploads")
        assert result.record.status is TenantStatus.ACTIVE
        assert [resource.resource_type for resource in resources] == [
            AwsService.SQS.value
        ]


def test_type_change_old_resource_is_a_leftover(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    stored_manifest = (ManifestEntry(type="s3-bucket", name="uploads", options={}),)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_manifest,
            catalog_version=catalog.catalog_version,
        )
        executor = _ReportsLeftover(calls, "uploads")
        service = _service(catalog, sqlite, executor, clock)
        plan = service.plan_update(TENANT_ID, _type_change_manifest())

        with pytest.raises(UpdateFailedError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        record = sqlite.get(TENANT_ID)
        assert "uploads" in str(caught.value)
        assert record.status is TenantStatus.UPDATE_FAILED
        assert record.manifest == stored.manifest


def test_removal_leftover_ends_update_failed_and_keeps_the_old_manifest(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        executor = _ReportsLeftover(calls, "jobs")
        service = _service(catalog, sqlite, executor, clock)
        plan = service.plan_update(TENANT_ID, _removed_manifest())

        with pytest.raises(UpdateFailedError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        record = sqlite.get(TENANT_ID)
        assert "jobs" in str(caught.value)
        assert record.status is TenantStatus.UPDATE_FAILED
        assert record.manifest == stored.manifest
        assert record.catalog_version == stored.catalog_version


def test_failed_provision_ends_update_failed_with_a_truncated_error(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        executor = _ProvisionExplodes()
        service = _service(catalog, sqlite, executor, clock)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        with pytest.raises(UpdateFailedError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        cause = caught.value.__cause__
        record = sqlite.get(TENANT_ID)
        assert isinstance(cause, ExecutorError)
        assert cause is executor.error
        assert str(caught.value) == truncate_error(str(cause))
        assert len(str(cause)) > ERROR_TEXT_LIMIT
        assert record.status is TenantStatus.UPDATE_FAILED
        assert record.last_error == truncate_error(str(cause))
        assert record.manifest == stored.manifest
        assert record.catalog_version == stored.catalog_version
        assert executor.destroy_calls == []


def test_corrected_update_from_update_failed_clears_last_error(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.UPDATE_FAILED,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _added_manifest())

        result = service.apply_update(plan, _confirmed(), _updater())

        assert stored.last_error == "provision failed"
        assert result.record.status is TenantStatus.ACTIVE
        assert result.record.last_error is None


def test_non_executor_error_stays_updating(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    executor = _KeyProvision()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _service(catalog, sqlite, executor, clock)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        with pytest.raises(KeyError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        assert caught.value is executor.error
        assert sqlite.get(TENANT_ID).status is TenantStatus.UPDATING
        assert executor.destroy_calls == []


def test_failed_activation_keeps_updating_and_the_new_resources(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        registry = _TransitionFails(sqlite, TenantStatus.ACTIVE)
        executor = RecordingExecutor(FakeExecutor(), sqlite, calls)
        service = _service(catalog, registry, executor, clock)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        with pytest.raises(RuntimeError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        resources = executor.list_tagged_resources(TENANT_ID)
        assert caught.value is registry.error
        assert sqlite.get(TENANT_ID).status is TenantStatus.UPDATING
        assert "destroy" not in calls
        assert "reports" in [resource.tags["component"] for resource in resources]


def test_type_change_requires_the_removals_confirmation(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    stored_manifest = (ManifestEntry(type="s3-bucket", name="uploads", options={}),)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        stored = _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=stored_manifest,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _type_change_manifest())
        refused = UpdateConfirmations(
            update=True,
            removals=False,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        assert "removals" in str(caught.value)
        assert calls == []
        assert sqlite.get(TENANT_ID) == stored


def test_applying_the_same_plan_twice_provisions_once(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    calls: list[str] = []
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, calls)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        first = service.apply_update(plan, _confirmed(), _updater())
        with pytest.raises(StalePlanError):
            service.apply_update(plan, _confirmed(), _updater())

        assert first.changed is True
        assert calls.count("provision") == 1


def test_one_error_names_the_missing_removals_and_replacement(
    tmp_path: Path,
) -> None:
    clock = MutableClock(T0)
    catalog = _replacement_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=_VERSIONED,
            catalog_version=catalog.catalog_version,
        )
        service = _update_service(sqlite, clock, catalog, [])
        plan = service.plan_update(TENANT_ID, _replaced_manifest(keep_jobs=False))
        refused = UpdateConfirmations(
            update=True,
            removals=False,
            replacements=frozenset(),
        )

        with pytest.raises(ConfirmationRequiredError) as caught:
            service.apply_update(plan, refused, _updater())

        message = str(caught.value)
        assert "removals" in message
        assert "uploads" in message


def test_failed_update_record_propagates(tmp_path: Path) -> None:
    clock = MutableClock(T0)
    catalog = build_catalog()
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        _seed(
            sqlite,
            status=TenantStatus.ACTIVE,
            manifest=tuple(_manifest().root),
            catalog_version=catalog.catalog_version,
        )
        registry = _TransitionFails(sqlite, TenantStatus.UPDATE_FAILED)
        executor = _ProvisionExplodes()
        service = _service(catalog, registry, executor, clock)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        with pytest.raises(RuntimeError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        assert caught.value is registry.error
        assert sqlite.get(TENANT_ID).status is TenantStatus.UPDATING

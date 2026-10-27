from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.executor import (
    ExecutorError,
    ProvisionRequest,
    ResourceRef,
)
from self_service_platform.core.manifest import Manifest, ManifestEntry
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.sweeper import (
    SweepEntry,
    Sweeper,
    SweepOutcome,
    SweepReport,
)
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
    TransitionRejectedError,
)
from self_service_platform.core.validator import validate_manifest
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
DEV_TTL = timedelta(days=1)
EPHEMERAL_TTL = timedelta(hours=3)
_MANIFEST = (
    ManifestEntry(type="s3-bucket", name="uploads", options={}),
    ManifestEntry(type="sqs-queue", name="jobs", options={}),
)
_CLI = Initiator(kind=InitiatorKind.CLI, owner="alice")
_SWEEPER = Initiator(kind=InitiatorKind.SWEEPER, owner="sweeper")
_STEPS: dict[TenantStatus, tuple[tuple[TenantStatus, TenantStatus], ...]] = {
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
    TenantStatus.DESTROY_FAILED: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        (TenantStatus.DESTROYING, TenantStatus.DESTROY_FAILED),
    ),
    TenantStatus.DESTROYED: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        (TenantStatus.DESTROYING, TenantStatus.DESTROYED),
    ),
}
_FROM_ACTIVE: dict[TenantStatus, tuple[tuple[TenantStatus, TenantStatus], ...]] = {
    TenantStatus.UPDATE_FAILED: (
        (TenantStatus.ACTIVE, TenantStatus.UPDATING),
        (TenantStatus.UPDATING, TenantStatus.UPDATE_FAILED),
    ),
    TenantStatus.DESTROY_FAILED: (
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        (TenantStatus.DESTROYING, TenantStatus.DESTROY_FAILED),
    ),
}


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Guarded:
    def __init__(self, inner: FakeExecutor | LocalExecutor) -> None:
        self._inner = inner
        self.destroy_calls: list[str] = []
        self.fail_ids: set[str] = set()
        self.key_error = False
        self.always_fail = False

    def provision(self, request: ProvisionRequest) -> None:
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self.destroy_calls.append(tenant_id)
        if self.key_error:
            raise KeyError("unknown")
        if self.always_fail or tenant_id in self.fail_ids:
            self.fail_ids.discard(tenant_id)
            raise ExecutorError("injected destroy failure")
        self._inner.destroy(tenant_id)

    def leave_one_resource_on_next_destroy(self) -> None:
        self._inner.leave_one_resource_on_next_destroy()

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return self._inner.tenant_role_exists(tenant_id)


class _ServiceTrap:
    def plan_destroy(self, tenant_id: str) -> None:
        raise AssertionError(tenant_id)

    def apply_destroy(
        self,
        plan: object,
        confirmed: object,
        initiator: object,
    ) -> None:
        raise AssertionError(confirmed)


class _ListOnly:
    def __init__(self, inner: SqliteRegistry) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        self.calls.append("list_tenants")
        return self._inner.list_tenants()

    def get(self, tenant_id: str) -> TenantRecord:
        raise AssertionError(tenant_id)

    def create_tenant(self, record: TenantRecord) -> None:
        raise AssertionError(record.tenant_id)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        raise AssertionError(new.value)


class _StaleOnSecondGet:
    def __init__(self, inner: SqliteRegistry, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._reads = 0

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._inner.list_tenants()

    def get(self, tenant_id: str) -> TenantRecord:
        record = self._inner.get(tenant_id)
        if tenant_id != self._tenant_id:
            return record
        self._reads += 1
        if self._reads >= 2:
            return record.model_copy(update={"status": TenantStatus.UPDATE_FAILED})
        return record

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        return self._inner.transition(tenant_id, expected, new, fields)


class _RecordingRegistry:
    def __init__(self, inner: SqliteRegistry, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        self._log.append("list_tenants")
        return self._inner.list_tenants()

    def get(self, tenant_id: str) -> TenantRecord:
        self._log.append("get")
        return self._inner.get(tenant_id)

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        self._log.append(f"transition {new.value}")
        return self._inner.transition(tenant_id, expected, new, fields)


class _RecordingExecutor:
    def __init__(self, inner: _Guarded, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def provision(self, request: ProvisionRequest) -> None:
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self._log.append("destroy")
        self._inner.destroy(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        self._log.append("list_tagged_resources")
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        self._log.append("tenant_role_exists")
        return self._inner.tenant_role_exists(tenant_id)


class _RejectDestroying:
    def __init__(self, inner: SqliteRegistry, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._inner.list_tenants()

    def get(self, tenant_id: str) -> TenantRecord:
        return self._inner.get(tenant_id)

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        if tenant_id == self._tenant_id and new is TenantStatus.DESTROYING:
            raise TransitionRejectedError("cannot transition from active to destroying")
        return self._inner.transition(tenant_id, expected, new, fields)


@dataclass
class World:
    registry: SqliteRegistry
    executor: _Guarded
    service: PlatformService
    catalog: Catalog


def _manifest() -> Manifest:
    return Manifest.model_validate(
        [{"type": entry.type, "name": entry.name} for entry in _MANIFEST]
    )


def _fixed(now: datetime) -> Callable[[], datetime]:
    return lambda: now


def _policy() -> TtlPolicy:
    return TtlPolicy(dev=DEV_TTL, ephemeral=EPHEMERAL_TTL)


@contextmanager
def _open_world(tmp_path: Path, kind: str) -> Iterator[World]:
    clock = MutableClock(T0)
    registry = SqliteRegistry(tmp_path / "tenants.sqlite", clock)
    try:
        inner: FakeExecutor | LocalExecutor
        if kind == "fake":
            inner = FakeExecutor()
        else:
            inner = LocalExecutor(tmp_path / "executor.sqlite")
        executor = _Guarded(inner)
        catalog = build_catalog()
        service = PlatformService(
            catalog=catalog,
            registry=registry,
            executor=executor,
            clock=clock,
            ttl_policy=_policy(),
        )
        yield World(
            registry=registry,
            executor=executor,
            service=service,
            catalog=catalog,
        )
    finally:
        registry.close()


def _activate(world: World, tenant_id: str, tenant_type: TenantType) -> TenantRecord:
    plan = world.service.plan_create(tenant_id, tenant_type, "alice", _manifest())
    result = world.service.apply_create(plan, True, _CLI)
    return result.record


def _fields(record: TenantRecord, last_error: str | None) -> TransitionFields:
    return TransitionFields(
        manifest=record.manifest,
        catalog_version=record.catalog_version,
        initiator=record.initiator,
        expires_at=record.expires_at,
        last_error=last_error,
    )


def _insert(world: World, record: TenantRecord) -> TenantRecord:
    world.registry.create_tenant(
        record.model_copy(update={"status": TenantStatus.CREATING})
    )
    current = record
    for expected, new in _STEPS[record.status]:
        last_error = record.last_error
        if new not in (TenantStatus.UPDATE_FAILED, TenantStatus.DESTROY_FAILED):
            last_error = None
        current = world.registry.transition(
            record.tenant_id,
            expected,
            new,
            _fields(record, last_error),
        )
    return current


def _provision(world: World, tenant_id: str, tenant_type: TenantType) -> None:
    world.executor.provision(
        ProvisionRequest(
            tenant_id=tenant_id,
            tenant_type=tenant_type,
            owner="alice",
            manifest=validate_manifest(_manifest(), world.catalog),
        )
    )


def _record(
    *,
    tenant_id: str,
    tenant_type: TenantType = TenantType.DEV,
    status: TenantStatus = TenantStatus.ACTIVE,
    expires_at: datetime | None = T0,
    initiator: Initiator | None = None,
    catalog_version: str,
    last_error: str | None = None,
) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        tenant_type=tenant_type,
        owner="alice",
        status=status,
        manifest=_MANIFEST,
        catalog_version=catalog_version,
        initiator=_CLI if initiator is None else initiator,
        expires_at=expires_at,
        last_error=last_error,
        updated_at=T0,
    )


def _service_on(
    world: World,
    registry: _StaleOnSecondGet | _RejectDestroying,
) -> PlatformService:
    return PlatformService(
        catalog=world.catalog,
        registry=registry,
        executor=world.executor,
        clock=_fixed(T0),
        ttl_policy=_policy(),
    )


def _sweep(
    world: World,
    now: datetime,
    *,
    dry_run: bool = False,
    service: object = None,
    max_destroy: int = 10,
) -> SweepReport:
    chosen = world.service if service is None else service
    return Sweeper(world.registry, _fixed(now), chosen).sweep(
        dry_run=dry_run,
        max_destroy=max_destroy,
    )


def _ids(executor: _Guarded, tenant_id: str) -> tuple[str, ...]:
    resources = executor.list_tagged_resources(tenant_id)
    return tuple(resource.identifier for resource in resources)


def test_sweeper_initiator_kind_round_trips_and_older_kinds_still_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tenants.sqlite"
    registry = SqliteRegistry(path, _fixed(T0))
    try:
        for tenant_id, kind in (
            ("legacy", InitiatorKind.CLI),
            ("agent-one", InitiatorKind.AGENT),
            ("swept", InitiatorKind.SWEEPER),
        ):
            registry.create_tenant(
                _record(
                    tenant_id=tenant_id,
                    status=TenantStatus.CREATING,
                    initiator=Initiator(kind=kind, owner=kind.value),
                    catalog_version="catalog-1",
                )
            )
    finally:
        registry.close()

    again = SqliteRegistry(path, _fixed(T0))
    try:
        legacy = again.get("legacy")
        agent = again.get("agent-one")
        swept = again.get("swept")
    finally:
        again.close()

    assert legacy.initiator == Initiator(kind=InitiatorKind.CLI, owner="cli")
    assert agent.initiator == Initiator(kind=InitiatorKind.AGENT, owner="agent")
    assert swept.initiator == _SWEEPER


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
@pytest.mark.parametrize(
    "tenant_type",
    [TenantType.DEV, TenantType.EPHEMERAL],
    ids=["dev", "ephemeral"],
)
def test_expired_tenant_is_destroyed(
    tmp_path: Path,
    executor_kind: str,
    tenant_type: TenantType,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        before = _activate(world, "demo", tenant_type)
        expires_at = before.expires_at
        assert expires_at is not None
        assert _ids(world.executor, "demo") == ("demo/jobs", "demo/uploads")
        assert world.executor.tenant_role_exists("demo")

        report = _sweep(world, expires_at, dry_run=False)
        record = world.registry.get("demo")

    assert report.entries == (
        SweepEntry(tenant_id="demo", outcome=SweepOutcome.DESTROYED, detail=""),
    )
    assert record.status is TenantStatus.DESTROYED
    assert record.initiator == _SWEEPER
    assert record.manifest == before.manifest
    assert record.expires_at == before.expires_at
    assert record.owner == before.owner
    assert record.last_error is None
    assert world.executor.destroy_calls == ["demo"]
    assert _ids(world.executor, "demo") == ()
    assert world.executor.tenant_role_exists("demo") is False


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
def test_a_tenant_that_has_not_expired_is_untouched(
    tmp_path: Path,
    executor_kind: str,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        before = _activate(world, "demo", TenantType.DEV)
        expires_at = before.expires_at
        assert expires_at is not None
        resources = _ids(world.executor, "demo")

        report = _sweep(world, expires_at - timedelta(seconds=1), dry_run=False)

        assert report.entries == ()
        assert world.registry.get("demo") == before
        assert world.executor.destroy_calls == []
        assert _ids(world.executor, "demo") == resources
        assert world.executor.tenant_role_exists("demo") is True


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
def test_expires_at_equal_to_now_is_expired_and_one_second_later_is_not(
    tmp_path: Path,
    executor_kind: str,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        due = _activate(world, "due", TenantType.DEV)
        now = due.expires_at
        assert now is not None
        later = _insert(
            world,
            _record(
                tenant_id="later",
                expires_at=now + timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _provision(world, "later", TenantType.DEV)

        report = _sweep(world, now, dry_run=False)

        assert report.entries == (
            SweepEntry(tenant_id="due", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert world.registry.get("due").status is TenantStatus.DESTROYED
        assert world.registry.get("later") == later
        assert world.executor.destroy_calls == ["due"]
        assert _ids(world.executor, "later") == ("later/jobs", "later/uploads")


def test_a_prod_tenant_is_never_swept(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        prod = _insert(
            world,
            _record(
                tenant_id="prod-one",
                tenant_type=TenantType.PROD,
                expires_at=T0 - timedelta(days=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _activate(world, "demo", TenantType.DEV)

        report = _sweep(world, T0 + DEV_TTL, dry_run=False)
        stored = world.registry.get("prod-one")

        assert prod.tenant_type is TenantType.PROD
        assert prod.expires_at == T0 - timedelta(days=1)
        assert [entry.tenant_id for entry in report.entries] == ["demo"]
        assert stored == prod
        assert "prod-one" not in world.executor.destroy_calls


def test_a_destroyed_tenant_is_ignored(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        gone = _insert(
            world,
            _record(
                tenant_id="gone",
                status=TenantStatus.DESTROYED,
                expires_at=T0 - timedelta(days=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _activate(world, "demo", TenantType.DEV)

        report = _sweep(world, T0 + DEV_TTL, dry_run=False)
        swept = world.registry.get("demo")

        assert [entry.tenant_id for entry in report.entries] == ["demo"]
        assert world.registry.get("gone") == gone
        assert gone.initiator.kind is InitiatorKind.CLI
        assert swept.status is TenantStatus.DESTROYED
        assert swept.initiator.kind is InitiatorKind.SWEEPER
        assert "gone" not in world.executor.destroy_calls


@pytest.mark.parametrize(
    "status",
    [TenantStatus.CREATING, TenantStatus.UPDATING, TenantStatus.DESTROYING],
    ids=["creating", "updating", "destroying"],
)
def test_an_expired_tenant_in_a_busy_status_is_skipped(
    tmp_path: Path,
    status: TenantStatus,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        before = _insert(
            world,
            _record(
                tenant_id="demo",
                status=status,
                expires_at=T0 - timedelta(days=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )

        report = _sweep(world, T0, dry_run=False)

        assert report.entries == (
            SweepEntry(
                tenant_id="demo",
                outcome=SweepOutcome.SKIPPED,
                detail=status.value,
            ),
        )
        assert world.registry.get("demo") == before
        assert world.executor.destroy_calls == []


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
@pytest.mark.parametrize(
    "status",
    [TenantStatus.UPDATE_FAILED, TenantStatus.DESTROY_FAILED],
    ids=["update_failed", "destroy_failed"],
)
def test_an_expired_failed_tenant_is_destroyed(
    tmp_path: Path,
    executor_kind: str,
    status: TenantStatus,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        created = _activate(world, "demo", TenantType.DEV)
        for expected, new in _FROM_ACTIVE[status]:
            created = world.registry.transition(
                "demo",
                expected,
                new,
                _fields(created, "provision failed"),
            )
        expires_at = created.expires_at
        assert expires_at is not None

        report = _sweep(world, expires_at, dry_run=False)
        record = world.registry.get("demo")

        assert report.entries == (
            SweepEntry(tenant_id="demo", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert record.status is TenantStatus.DESTROYED
        assert record.initiator == _SWEEPER
        assert record.last_error is None
        assert _ids(world.executor, "demo") == ()
        assert world.executor.tenant_role_exists("demo") is False


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
def test_a_destroy_failure_does_not_stop_the_next_tenant(
    tmp_path: Path,
    executor_kind: str,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        _activate(world, "alpha", TenantType.DEV)
        _activate(world, "beta", TenantType.DEV)
        world.executor.fail_ids.add("alpha")

        report = _sweep(world, T0 + DEV_TTL, dry_run=False)

        assert report.entries == (
            SweepEntry(
                tenant_id="alpha",
                outcome=SweepOutcome.FAILED,
                detail="injected destroy failure",
            ),
            SweepEntry(tenant_id="beta", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert world.registry.get("alpha").status is TenantStatus.DESTROY_FAILED
        assert world.registry.get("beta").status is TenantStatus.DESTROYED
        assert world.registry.get("beta").initiator == _SWEEPER
        assert world.executor.destroy_calls == ["alpha", "beta"]


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
def test_a_leftover_resource_is_reported_failed(
    tmp_path: Path,
    executor_kind: str,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        before = _activate(world, "demo", TenantType.DEV)
        world.executor.leave_one_resource_on_next_destroy()
        expires_at = before.expires_at
        assert expires_at is not None

        report = _sweep(world, expires_at, dry_run=False)
        record = world.registry.get("demo")

        assert report.entries == (
            SweepEntry(
                tenant_id="demo",
                outcome=SweepOutcome.FAILED,
                detail="leftover resources remain: demo/uploads",
            ),
        )
        assert record.status is TenantStatus.DESTROY_FAILED
        assert record.last_error == "leftover resources remain: demo/uploads"
        assert _ids(world.executor, "demo") == ("demo/uploads",)
        assert world.executor.tenant_role_exists("demo") is False


def test_dry_run_lists_the_registry_and_does_not_touch_the_service(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        before = _activate(world, "demo", TenantType.DEV)
        expires_at = before.expires_at
        assert expires_at is not None
        resources = _ids(world.executor, "demo")
        listing = _ListOnly(world.registry)

        report = Sweeper(listing, _fixed(expires_at), _ServiceTrap()).sweep()

        assert listing.calls == ["list_tenants"]
        assert report.entries == (
            SweepEntry(
                tenant_id="demo",
                outcome=SweepOutcome.SKIPPED,
                detail="dry run",
            ),
        )
        assert world.registry.get("demo") == before
        assert world.executor.destroy_calls == []
        assert _ids(world.executor, "demo") == resources


def test_dry_run_reports_every_eligible_tenant(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        early = _insert(
            world,
            _record(
                tenant_id="early",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        later = _insert(
            world,
            _record(
                tenant_id="later",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        last = _insert(
            world,
            _record(
                tenant_id="last",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )

        report = _sweep(world, T0, dry_run=True, max_destroy=1)

        assert report.entries == (
            SweepEntry(
                tenant_id="early", outcome=SweepOutcome.SKIPPED, detail="dry run"
            ),
            SweepEntry(
                tenant_id="last", outcome=SweepOutcome.SKIPPED, detail="dry run"
            ),
            SweepEntry(
                tenant_id="later", outcome=SweepOutcome.SKIPPED, detail="dry run"
            ),
        )
        assert world.registry.get("early") == early
        assert world.registry.get("last") == last
        assert world.registry.get("later") == later
        assert world.executor.destroy_calls == []


def test_the_clock_is_read_once_per_sweep(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        _insert(
            world,
            _record(
                tenant_id="due",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        later = _insert(
            world,
            _record(
                tenant_id="later",
                expires_at=T0 + timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        calls: list[datetime] = []

        def clock() -> datetime:
            calls.append(T0 if len(calls) == 0 else T0 + timedelta(days=30))
            return calls[-1]

        report = Sweeper(world.registry, clock, world.service).sweep(dry_run=False)

        assert calls == [T0]
        assert report.entries == (
            SweepEntry(tenant_id="due", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert world.registry.get("later") == later


def test_a_second_sweep_does_nothing(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        created = _activate(world, "demo", TenantType.DEV)
        expires_at = created.expires_at
        assert expires_at is not None
        first = _sweep(world, expires_at, dry_run=False)
        calls = list(world.executor.destroy_calls)
        destroyed = world.registry.get("demo")

        second = _sweep(world, expires_at, dry_run=False)

        assert first.entries[0].outcome is SweepOutcome.DESTROYED
        assert second.entries == ()
        assert world.executor.destroy_calls == calls
        assert world.registry.get("demo") == destroyed


def test_a_stale_plan_is_skipped_and_the_next_tenant_is_destroyed(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        early = _insert(
            world,
            _record(
                tenant_id="early",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _insert(
            world,
            _record(
                tenant_id="later",
                expires_at=T0 + timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        wrapper = _StaleOnSecondGet(world.registry, "early")
        service = _service_on(world, wrapper)

        report = Sweeper(wrapper, _fixed(T0 + timedelta(seconds=1)), service).sweep(
            dry_run=False
        )

        assert report.entries == (
            SweepEntry(
                tenant_id="early",
                outcome=SweepOutcome.SKIPPED,
                detail="stored status does not match the plan",
            ),
            SweepEntry(tenant_id="later", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert world.registry.get("early") == early
        assert world.registry.get("later").status is TenantStatus.DESTROYED
        assert "early" not in world.executor.destroy_calls


def test_a_lost_conditional_write_is_skipped_and_the_next_tenant_is_destroyed(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        early = _insert(
            world,
            _record(
                tenant_id="early",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _insert(
            world,
            _record(
                tenant_id="later",
                expires_at=T0 + timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        wrapper = _RejectDestroying(world.registry, "early")
        service = _service_on(world, wrapper)

        report = Sweeper(wrapper, _fixed(T0 + timedelta(seconds=1)), service).sweep(
            dry_run=False
        )

        assert report.entries == (
            SweepEntry(
                tenant_id="early",
                outcome=SweepOutcome.SKIPPED,
                detail="cannot transition from active to destroying",
            ),
            SweepEntry(tenant_id="later", outcome=SweepOutcome.DESTROYED, detail=""),
        )
        assert world.registry.get("early") == early
        assert world.registry.get("later").status is TenantStatus.DESTROYED


@pytest.mark.parametrize("executor_kind", ["fake", "local"])
def test_an_unexpected_executor_error_propagates_and_the_tenant_stays_destroying(
    tmp_path: Path,
    executor_kind: str,
) -> None:
    with _open_world(tmp_path, executor_kind) as world:
        created = _activate(world, "demo", TenantType.DEV)
        expires_at = created.expires_at
        assert expires_at is not None
        world.executor.key_error = True

        with pytest.raises(KeyError, match="unknown"):
            _sweep(world, expires_at, dry_run=False)

        assert world.registry.get("demo").status is TenantStatus.DESTROYING
        assert world.executor.destroy_calls == ["demo"]


def test_a_real_sweep_without_a_service_raises_before_listing(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        before = _activate(world, "demo", TenantType.DEV)
        listing = _ListOnly(world.registry)

        with pytest.raises(
            ValueError, match="^a service is required when dry_run is false$"
        ):
            Sweeper(listing, _fixed(T0)).sweep(dry_run=False)

        assert listing.calls == []
        assert world.registry.get("demo") == before
        assert world.executor.destroy_calls == []


def test_a_swept_tenant_follows_the_destroy_path(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        _insert(
            world,
            _record(
                tenant_id="demo",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        log: list[str] = []
        registry = _RecordingRegistry(world.registry, log)
        executor = _RecordingExecutor(world.executor, log)
        service = PlatformService(
            catalog=world.catalog,
            registry=registry,
            executor=executor,
            clock=_fixed(T0),
            ttl_policy=_policy(),
        )

        Sweeper(registry, _fixed(T0), service).sweep(dry_run=False)

        assert [
            entry
            for entry in log
            if entry.startswith("transition")
            or entry in {"destroy", "list_tagged_resources", "tenant_role_exists"}
        ] == [
            "transition destroying",
            "destroy",
            "list_tagged_resources",
            "tenant_role_exists",
            "transition destroyed",
        ]


def test_report_entries_are_sorted_by_expires_at_then_tenant_id(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        for tenant_id, expires_at in (
            ("late", T0 + timedelta(hours=1)),
            ("m", T0),
            ("early", T0),
            ("a", T0),
        ):
            _insert(
                world,
                _record(
                    tenant_id=tenant_id,
                    expires_at=expires_at,
                    catalog_version=world.catalog.catalog_version,
                ),
            )

        report = _sweep(world, T0 + timedelta(hours=1), dry_run=True)

        assert [entry.tenant_id for entry in report.entries] == [
            "a",
            "early",
            "m",
            "late",
        ]


def test_sweep_stops_after_max_destroy(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        stuck = _insert(
            world,
            _record(
                tenant_id="s",
                status=TenantStatus.CREATING,
                expires_at=T0 - timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _insert(
            world,
            _record(
                tenant_id="a",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        _insert(
            world,
            _record(
                tenant_id="b",
                expires_at=T0 + timedelta(seconds=1),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        waiting = _insert(
            world,
            _record(
                tenant_id="c",
                expires_at=T0 + timedelta(seconds=2),
                catalog_version=world.catalog.catalog_version,
            ),
        )
        world.executor.fail_ids.add("a")

        report = _sweep(world, T0 + timedelta(seconds=2), dry_run=False, max_destroy=1)

        assert report.entries == (
            SweepEntry(tenant_id="s", outcome=SweepOutcome.SKIPPED, detail="creating"),
            SweepEntry(
                tenant_id="a",
                outcome=SweepOutcome.FAILED,
                detail="injected destroy failure",
            ),
            SweepEntry(tenant_id="b", outcome=SweepOutcome.DESTROYED, detail=""),
            SweepEntry(
                tenant_id="c", outcome=SweepOutcome.SKIPPED, detail="limit reached"
            ),
        )
        assert world.registry.get("s") == stuck
        assert world.registry.get("a").status is TenantStatus.DESTROY_FAILED
        assert world.registry.get("b").status is TenantStatus.DESTROYED
        assert world.registry.get("c") == waiting
        assert world.executor.destroy_calls == ["a", "b"]


def test_max_destroy_counts_two_successful_destroys(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        for tenant_id in ("e", "d", "c", "b", "a"):
            _insert(
                world,
                _record(
                    tenant_id=tenant_id,
                    expires_at=T0,
                    catalog_version=world.catalog.catalog_version,
                ),
            )

        report = _sweep(world, T0, dry_run=False, max_destroy=2)

        assert [entry.tenant_id for entry in report.entries] == [
            "a",
            "b",
            "c",
            "d",
            "e",
        ]
        assert [entry.outcome for entry in report.entries] == [
            SweepOutcome.DESTROYED,
            SweepOutcome.DESTROYED,
            SweepOutcome.SKIPPED,
            SweepOutcome.SKIPPED,
            SweepOutcome.SKIPPED,
        ]
        assert [entry.detail for entry in report.entries[2:]] == ["limit reached"] * 3
        assert world.executor.destroy_calls == ["a", "b"]
        assert world.registry.get("c").status is TenantStatus.ACTIVE


def test_default_max_destroy_is_ten(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        names = [f"n{index:02d}" for index in range(1, 12)]
        for name in names:
            _insert(
                world,
                _record(
                    tenant_id=name,
                    expires_at=T0,
                    catalog_version=world.catalog.catalog_version,
                ),
            )

        report = Sweeper(world.registry, _fixed(T0), world.service).sweep(dry_run=False)

        assert [entry.tenant_id for entry in report.entries] == names
        assert [entry.outcome for entry in report.entries[:10]] == [
            SweepOutcome.DESTROYED
        ] * 10
        assert report.entries[10] == SweepEntry(
            tenant_id="n11",
            outcome=SweepOutcome.SKIPPED,
            detail="limit reached",
        )
        assert world.registry.get("n11").status is TenantStatus.ACTIVE
        assert world.executor.destroy_calls == names[:10]


def test_a_tenant_that_stays_destroy_failed_is_retried_on_the_next_sweep(
    tmp_path: Path,
) -> None:
    with _open_world(tmp_path, "fake") as world:
        _insert(
            world,
            _record(
                tenant_id="demo",
                expires_at=T0,
                catalog_version=world.catalog.catalog_version,
            ),
        )
        world.executor.always_fail = True

        first = _sweep(world, T0, dry_run=False)
        second = _sweep(world, T0, dry_run=False)
        record = world.registry.get("demo")

        assert first.entries == (
            SweepEntry(
                tenant_id="demo",
                outcome=SweepOutcome.FAILED,
                detail="injected destroy failure",
            ),
        )
        assert second.entries == first.entries
        assert record.status is TenantStatus.DESTROY_FAILED
        assert world.executor.destroy_calls == ["demo", "demo"]


def test_a_naive_clock_is_rejected(tmp_path: Path) -> None:
    with _open_world(tmp_path, "fake") as world:
        before = _activate(world, "demo", TenantType.DEV)

        def naive() -> datetime:
            return datetime(2026, 10, 26, 12, 0)

        with pytest.raises(ValueError, match="^clock must return timezone-aware UTC$"):
            Sweeper(world.registry, naive, world.service).sweep(dry_run=False)
        with pytest.raises(ValueError, match="^clock must return timezone-aware UTC$"):
            Sweeper(world.registry, naive, world.service).sweep(dry_run=True)

        assert world.registry.get("demo") == before
        assert world.executor.destroy_calls == []

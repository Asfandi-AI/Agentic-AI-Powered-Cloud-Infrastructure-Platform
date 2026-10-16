import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from self_service_platform.core.manifest import ManifestEntry
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
    TransitionRejectedError,
)
from self_service_platform.registry.sqlite import SqliteRegistry

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 11, 13, 0, tzinfo=UTC)
TENANT_ID = "tenant-a"
HOSTILE_TENANT_ID = "x'; DROP TABLE tenants;--"

APPLIED_MANIFEST = (
    ManifestEntry(
        type="s3-bucket",
        name="uploads",
        options={"versioning": True},
    ),
)
REPLACEMENT_MANIFEST = (
    ManifestEntry(
        type="sqs-queue",
        name="jobs",
        options={"fifo": False},
    ),
)

_ALLOWED = frozenset(
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

_STEPS_TO: dict[TenantStatus, tuple[tuple[TenantStatus, TenantStatus], ...]] = {
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


def _initiator(
    kind: InitiatorKind = InitiatorKind.CLI,
    owner: str = "alice",
) -> Initiator:
    return Initiator(kind=kind, owner=owner)


def _record(
    *,
    tenant_id: str = TENANT_ID,
    tenant_type: TenantType = TenantType.DEV,
    owner: str = "alice",
    status: TenantStatus = TenantStatus.CREATING,
    manifest: tuple[ManifestEntry, ...] = APPLIED_MANIFEST,
    catalog_version: str = "catalog-1",
    initiator: Initiator | None = None,
    expires_at: datetime | None = T0,
    last_error: str | None = None,
    updated_at: datetime = T0,
) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        tenant_type=tenant_type,
        status=status,
        manifest=manifest,
        catalog_version=catalog_version,
        owner=owner,
        initiator=_initiator() if initiator is None else initiator,
        expires_at=expires_at,
        last_error=last_error,
        updated_at=updated_at,
    )


def _fields(
    *,
    manifest: tuple[ManifestEntry, ...] = APPLIED_MANIFEST,
    catalog_version: str = "catalog-1",
    initiator: Initiator | None = None,
    expires_at: datetime | None = T0,
    last_error: str | None = None,
) -> TransitionFields:
    return TransitionFields(
        manifest=manifest,
        catalog_version=catalog_version,
        initiator=_initiator() if initiator is None else initiator,
        expires_at=expires_at,
        last_error=last_error,
    )


def _walk(
    registry: SqliteRegistry,
    tenant_id: str,
    status: TenantStatus,
    *,
    manifest: tuple[ManifestEntry, ...] = APPLIED_MANIFEST,
    catalog_version: str = "catalog-1",
    initiator: Initiator | None = None,
    expires_at: datetime | None = T0,
) -> None:
    for expected, new in _STEPS_TO[status]:
        last_error = "provision failed" if new == TenantStatus.UPDATE_FAILED else None
        registry.transition(
            tenant_id,
            expected,
            new,
            _fields(
                manifest=manifest,
                catalog_version=catalog_version,
                initiator=initiator,
                expires_at=expires_at,
                last_error=last_error,
            ),
        )


def _clock(*moments: datetime) -> Callable[[], datetime]:
    remaining = list(moments)

    def clock() -> datetime:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return clock


@pytest.mark.parametrize(
    ("current", "new"),
    [
        pytest.param(current, new, id=f"{current.value}-to-{new.value}")
        for current, new in sorted(
            _ALLOWED, key=lambda pair: (pair[0].value, pair[1].value)
        )
    ],
)
def test_allowed_transition_persists_the_new_status(
    tmp_path: Path,
    current: TenantStatus,
    new: TenantStatus,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(_record())
        _walk(registry, TENANT_ID, current)
        stored = registry.transition(TENANT_ID, current, new, _fields())
        loaded = registry.get(TENANT_ID)

    assert stored.status == new
    assert loaded.status == new


@pytest.mark.parametrize(
    ("current", "new"),
    [
        pytest.param(current, new, id=f"{current.value}-to-{new.value}")
        for current in TenantStatus
        for new in TenantStatus
        if (current, new) not in _ALLOWED
    ],
)
def test_forbidden_transition_is_rejected_and_record_is_unchanged(
    tmp_path: Path,
    current: TenantStatus,
    new: TenantStatus,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T1)) as registry:
        registry.create_tenant(_record(last_error="kept"))
        _walk(registry, TENANT_ID, current)
        before = registry.get(TENANT_ID)
        with pytest.raises(TransitionRejectedError):
            registry.transition(
                TENANT_ID,
                current,
                new,
                _fields(
                    manifest=REPLACEMENT_MANIFEST,
                    catalog_version="catalog-9",
                    initiator=_initiator(InitiatorKind.AGENT, "mallory"),
                    expires_at=T1,
                    last_error="should-not-stick",
                ),
            )
        assert registry.get(TENANT_ID) == before


def test_stale_expected_status_is_rejected_and_record_is_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(_record())
        _walk(registry, TENANT_ID, TenantStatus.ACTIVE)
        registry.transition(
            TENANT_ID,
            TenantStatus.ACTIVE,
            TenantStatus.UPDATING,
            _fields(initiator=_initiator(InitiatorKind.CLI, "carol")),
        )
        before = registry.get(TENANT_ID)
        with pytest.raises(TransitionRejectedError):
            registry.transition(
                TENANT_ID,
                TenantStatus.ACTIVE,
                TenantStatus.UPDATING,
                _fields(
                    manifest=REPLACEMENT_MANIFEST,
                    catalog_version="catalog-9",
                    initiator=_initiator(InitiatorKind.AGENT, "mallory"),
                    last_error="should-not-stick",
                ),
            )
        assert registry.get(TENANT_ID) == before


def test_transition_of_missing_tenant_raises_tenant_not_found(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        with pytest.raises(TenantNotFoundError):
            registry.transition(
                "missing",
                TenantStatus.ACTIVE,
                TenantStatus.UPDATING,
                _fields(),
            )
        with pytest.raises(TenantNotFoundError):
            registry.get("missing")


def test_two_connections_racing_active_to_updating_let_exactly_one_succeed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(_record())
        _walk(registry, TENANT_ID, TenantStatus.ACTIVE)

    barrier = threading.Barrier(2)

    def attempt() -> bool:
        with SqliteRegistry(path, _clock(T1)) as registry:
            barrier.wait(timeout=10)
            try:
                registry.transition(
                    TENANT_ID,
                    TenantStatus.ACTIVE,
                    TenantStatus.UPDATING,
                    _fields(),
                )
            except TransitionRejectedError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1

    with SqliteRegistry(path, _clock(T0)) as registry:
        assert registry.get(TENANT_ID).status == TenantStatus.UPDATING


def test_update_failed_keeps_applied_manifest_and_stores_the_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(
            _record(manifest=APPLIED_MANIFEST, catalog_version="catalog-1")
        )
        _walk(
            registry,
            TENANT_ID,
            TenantStatus.UPDATING,
            manifest=APPLIED_MANIFEST,
            catalog_version="catalog-1",
        )
        registry.transition(
            TENANT_ID,
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _fields(
                manifest=REPLACEMENT_MANIFEST,
                catalog_version="catalog-2",
                last_error="stack failed",
            ),
        )
        stored = registry.get(TENANT_ID)

    assert stored.status == TenantStatus.UPDATE_FAILED
    assert stored.manifest == APPLIED_MANIFEST
    assert stored.catalog_version == "catalog-1"
    assert stored.last_error == "stack failed"


def test_updating_to_active_clears_last_error(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(
            _record(manifest=APPLIED_MANIFEST, catalog_version="catalog-1")
        )
        _walk(
            registry,
            TENANT_ID,
            TenantStatus.UPDATING,
            manifest=APPLIED_MANIFEST,
            catalog_version="catalog-1",
        )
        registry.transition(
            TENANT_ID,
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _fields(last_error="stack failed"),
        )
        registry.transition(
            TENANT_ID,
            TenantStatus.UPDATE_FAILED,
            TenantStatus.UPDATING,
            _fields(last_error="stack failed"),
        )
        assert registry.get(TENANT_ID).last_error == "stack failed"
        registry.transition(
            TENANT_ID,
            TenantStatus.UPDATING,
            TenantStatus.ACTIVE,
            _fields(
                manifest=REPLACEMENT_MANIFEST,
                catalog_version="catalog-2",
                last_error=None,
            ),
        )
        stored = registry.get(TENANT_ID)

    assert stored.status == TenantStatus.ACTIVE
    assert stored.last_error is None
    assert stored.manifest == REPLACEMENT_MANIFEST
    assert stored.catalog_version == "catalog-2"


def test_long_error_is_truncated_to_the_limit_and_keeps_its_beginning(
    tmp_path: Path,
) -> None:
    error = "provision-failed:" + ("x" * ERROR_TEXT_LIMIT)
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(_record())
        _walk(registry, TENANT_ID, TenantStatus.UPDATING)
        registry.transition(
            TENANT_ID,
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _fields(last_error=error),
        )
        stored = registry.get(TENANT_ID)

    assert ERROR_TEXT_LIMIT == 1024
    assert stored.last_error is not None
    assert len(stored.last_error) <= ERROR_TEXT_LIMIT
    assert stored.last_error == error[:ERROR_TEXT_LIMIT]
    assert stored.last_error.startswith("provision-failed:")


def test_initiator_is_stored_on_each_transition(tmp_path: Path) -> None:
    steps = (
        (
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            _initiator(InitiatorKind.AGENT, "bob"),
        ),
        (
            TenantStatus.ACTIVE,
            TenantStatus.UPDATING,
            _initiator(InitiatorKind.CLI, "carol"),
        ),
        (
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _initiator(InitiatorKind.AGENT, "dave"),
        ),
        (
            TenantStatus.UPDATE_FAILED,
            TenantStatus.UPDATING,
            _initiator(InitiatorKind.CLI, "eve"),
        ),
        (
            TenantStatus.UPDATING,
            TenantStatus.ACTIVE,
            _initiator(InitiatorKind.AGENT, "frank"),
        ),
        (
            TenantStatus.ACTIVE,
            TenantStatus.DESTROYING,
            _initiator(InitiatorKind.CLI, "grace"),
        ),
    )
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(_record(status=TenantStatus.CREATING))
        for expected, new, initiator in steps:
            last_error = "failed" if new == TenantStatus.UPDATE_FAILED else None
            registry.transition(
                TENANT_ID,
                expected,
                new,
                _fields(initiator=initiator, last_error=last_error),
            )
            assert registry.get(TENANT_ID).initiator == initiator


def test_owner_stays_the_create_value_across_the_lifecycle(tmp_path: Path) -> None:
    steps = (
        (
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            _initiator(InitiatorKind.AGENT, "bob"),
        ),
        (
            TenantStatus.ACTIVE,
            TenantStatus.UPDATING,
            _initiator(InitiatorKind.CLI, "carol"),
        ),
        (
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _initiator(InitiatorKind.AGENT, "dave"),
        ),
        (
            TenantStatus.UPDATE_FAILED,
            TenantStatus.UPDATING,
            _initiator(InitiatorKind.CLI, "eve"),
        ),
        (
            TenantStatus.UPDATING,
            TenantStatus.ACTIVE,
            _initiator(InitiatorKind.AGENT, "frank"),
        ),
    )
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(
            _record(owner="alice", initiator=_initiator(InitiatorKind.CLI, "zoe"))
        )
        assert registry.get(TENANT_ID).owner == "alice"
        for expected, new, initiator in steps:
            last_error = "failed" if new == TenantStatus.UPDATE_FAILED else None
            registry.transition(
                TENANT_ID,
                expected,
                new,
                _fields(initiator=initiator, last_error=last_error),
            )
            assert registry.get(TENANT_ID).owner == "alice"


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(status, id=status.value)
        for status in TenantStatus
        if status is not TenantStatus.CREATING
    ],
)
def test_create_rejects_a_status_other_than_creating(
    tmp_path: Path,
    status: TenantStatus,
) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        with pytest.raises(TransitionRejectedError):
            registry.create_tenant(_record(status=status))
        with pytest.raises(TenantNotFoundError):
            registry.get(TENANT_ID)


def test_duplicate_tenant_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    original = _record(catalog_version="catalog-1")
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(original)
        with pytest.raises(DuplicateTenantError):
            registry.create_tenant(_record(catalog_version="catalog-2"))
        assert registry.get(original.tenant_id) == original


def test_hostile_tenant_id_is_stored_as_data(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(_record(tenant_id=HOSTILE_TENANT_ID))
        registry.transition(
            HOSTILE_TENANT_ID,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            _fields(),
        )
        registry.create_tenant(_record(tenant_id="tenant-b"))
        assert registry.get(HOSTILE_TENANT_ID).tenant_id == HOSTILE_TENANT_ID
        assert registry.get(HOSTILE_TENANT_ID).status == TenantStatus.ACTIVE
        assert registry.get("tenant-b").tenant_id == "tenant-b"

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("tenants",),
        ).fetchone()
    assert row == ("tenants",)


def test_stored_record_round_trips_including_nested_option_values(
    tmp_path: Path,
) -> None:
    manifest = (
        ManifestEntry(
            type="s3-bucket",
            name="uploads",
            options={
                "versioning": True,
                "lifecycle": {"enabled": False, "days": 30, "prefix": "logs/"},
            },
        ),
        ManifestEntry(
            type="sqs-queue",
            name="jobs",
            options={"fifo": False},
        ),
    )
    updated_at = datetime(2026, 10, 11, 18, 30, tzinfo=UTC)
    record = _record(
        tenant_type=TenantType.PROD,
        manifest=manifest,
        catalog_version="catalog-7",
        initiator=_initiator(InitiatorKind.AGENT, "bob"),
        expires_at=None,
        last_error="disk full",
        updated_at=updated_at,
    )
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(updated_at)) as registry:
        registry.create_tenant(record)
        loaded = registry.get(record.tenant_id)

    assert loaded == record
    assert isinstance(loaded.manifest, tuple)
    assert loaded.manifest[0].options["versioning"] is True
    assert loaded.manifest[0].options["lifecycle"] == {
        "enabled": False,
        "days": 30,
        "prefix": "logs/",
    }
    assert loaded.manifest[1].options["fifo"] is False


def test_writes_take_updated_at_from_the_clock(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    record = _record(
        status=TenantStatus.CREATING,
        updated_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    with SqliteRegistry(path, _clock(T0, T1)) as registry:
        registry.create_tenant(record)
        assert registry.get(TENANT_ID).updated_at == T0
        registry.transition(
            TENANT_ID,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            _fields(),
        )
        assert registry.get(TENANT_ID).updated_at == T1


def test_expires_at_and_updated_at_are_stored_as_iso8601_text(tmp_path: Path) -> None:
    expires_at = datetime(2026, 10, 12, 8, 15, 30, tzinfo=UTC)
    updated_at = datetime(2026, 10, 11, 18, 45, tzinfo=UTC)
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(updated_at)) as registry:
        registry.create_tenant(_record(expires_at=expires_at, updated_at=updated_at))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT expires_at, updated_at FROM tenants WHERE tenant_id = ?",
            (TENANT_ID,),
        ).fetchone()

    assert row is not None
    stored_expires, stored_updated = row
    assert stored_expires == expires_at.isoformat()
    assert stored_updated == updated_at.isoformat()


def test_non_utc_offset_reads_back_as_the_same_utc_instant(tmp_path: Path) -> None:
    given = datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=5)))
    expected = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    record = _record(expires_at=given, updated_at=given)
    fields = _fields(expires_at=given)
    assert record.expires_at == expected
    assert record.expires_at.utcoffset() == timedelta(0)
    assert record.updated_at == expected
    assert record.updated_at.utcoffset() == timedelta(0)
    assert fields.expires_at == expected
    assert fields.expires_at.utcoffset() == timedelta(0)

    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(record)
        created = registry.get(TENANT_ID)
        assert created.expires_at == expected
        assert created.expires_at.utcoffset() == timedelta(0)
        registry.transition(
            TENANT_ID,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            fields,
        )
        loaded = registry.get(TENANT_ID)

    assert loaded.expires_at == expected
    assert loaded.expires_at.utcoffset() == timedelta(0)


def test_stored_expires_at_text_orders_as_the_instants(tmp_path: Path) -> None:
    earlier_given = datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=5)))
    later_given = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    earlier = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    later = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    path = tmp_path / "registry.sqlite"
    with SqliteRegistry(path, _clock(T0)) as registry:
        registry.create_tenant(_record(tenant_id="earlier", expires_at=earlier_given))
        registry.create_tenant(_record(tenant_id="later", expires_at=later_given))

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT tenant_id, expires_at FROM tenants ORDER BY expires_at"
        ).fetchall()

    assert [row[0] for row in rows] == ["earlier", "later"]
    assert [row[1] for row in rows] == [earlier.isoformat(), later.isoformat()]
    assert [datetime.fromisoformat(row[1]) for row in rows] == [earlier, later]


def test_close_leaves_stored_tenants_readable(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite"
    record = _record()
    registry = SqliteRegistry(path, _clock(T0))
    registry.create_tenant(record)
    registry.close()

    with SqliteRegistry(path, _clock(T0)) as again:
        assert again.get(record.tenant_id) == record

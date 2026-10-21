import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from self_service_platform.core.executor import (
    Executor,
    ExecutorError,
    ProvisionRequest,
)
from self_service_platform.core.service import DestroyFailedError, UpdateFailedError
from self_service_platform.core.tenant import TenantStatus
from self_service_platform.registry.sqlite import SqliteRegistry
from tests.core.test_service import (
    T0,
    TENANT_ID,
    MutableClock,
    _added_manifest,
    _arm,
    _catalog_seed,
    _confirmed,
    _service,
    _updater,
)
from tests.executors.test_executor_contract import _manifest, _request

_DATABASE_NAME = "local-executor.sqlite"
_TABLES = ("resources", "object_keys", "tenant_roles")
_COLUMNS = frozenset({"tenant_id", "component"})


def _open(path: Path) -> Executor:
    from self_service_platform.executors.local import LocalExecutor

    return LocalExecutor(path)


def _database(directory: Path) -> Path:
    return directory / _DATABASE_NAME


def _count_rows(path: Path, table: str, column: str, value: str) -> int:
    if table not in _TABLES or column not in _COLUMNS:
        raise ValueError(f"unexpected query {table}.{column}")
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
            (value,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _user_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


class _CorruptAfterDestroy:
    def __init__(self, inner: Executor, path: Path) -> None:
        self._inner = inner
        self._path = path

    def provision(self, request: ProvisionRequest) -> None:
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self._inner.destroy(tenant_id)
        self._path.write_bytes(os.urandom(64))

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[object, ...]:
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return self._inner.tenant_role_exists(tenant_id)


def test_missing_executor_file_reads_as_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "absent")
    executor = _open(database)

    assert executor.list_tagged_resources("tenant-a") == ()
    assert executor.tenant_role_exists("tenant-a") is False
    assert not database.exists()
    assert not database.parent.exists()


def test_unreadable_executor_file_raises_on_read(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(os.urandom(64))
    executor = _open(database)

    with pytest.raises(ExecutorError) as listed:
        executor.list_tagged_resources("tenant-a")
    with pytest.raises(ExecutorError) as role:
        executor.tenant_role_exists("tenant-a")

    assert isinstance(listed.value.__cause__, sqlite3.Error | OSError)
    assert isinstance(role.value.__cause__, sqlite3.Error | OSError)


def test_zero_byte_database_reads_as_empty(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(b"")
    modified = database.stat().st_mtime_ns
    executor = _open(database)

    assert executor.list_tagged_resources("tenant-a") == ()
    assert executor.tenant_role_exists("tenant-a") is False
    assert database.read_bytes() == b""
    assert database.stat().st_mtime_ns == modified
    names = {path.name for path in tmp_path.iterdir()}
    assert f"{_DATABASE_NAME}-journal" not in names
    assert f"{_DATABASE_NAME}-wal" not in names
    assert f"{_DATABASE_NAME}-shm" not in names


def test_database_without_executor_tables_raises_on_read(tmp_path: Path) -> None:
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE other (id TEXT)")
    connection.commit()
    connection.close()
    assert database.stat().st_size > 0
    executor = _open(database)

    with pytest.raises(ExecutorError):
        executor.list_tagged_resources("tenant-a")
    with pytest.raises(ExecutorError):
        executor.tenant_role_exists("tenant-a")

    assert set(_TABLES).isdisjoint(_user_tables(database))


def test_zero_byte_file_accepts_a_provision(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(b"")
    request = _request(_manifest(("sqs-queue", "jobs")))
    executor = _open(database)

    executor.provision(request)

    seen = executor.list_tagged_resources(request.tenant_id)
    assert [resource.tags["component"] for resource in seen] == ["jobs"]
    assert executor.tenant_role_exists(request.tenant_id) is True


def test_corrupted_file_makes_provision_raise_executor_error(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(os.urandom(64))
    executor = _open(database)

    with pytest.raises(ExecutorError) as caught:
        executor.provision(_request(_manifest(("sqs-queue", "jobs"))))

    assert isinstance(caught.value.__cause__, sqlite3.Error | OSError)


def test_corrupted_file_makes_destroy_raise_executor_error(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(os.urandom(64))
    executor = _open(database)

    with pytest.raises(ExecutorError) as caught:
        executor.destroy("tenant-a")

    assert isinstance(caught.value.__cause__, sqlite3.Error | OSError)


def test_reads_do_not_modify_the_database_file(tmp_path: Path) -> None:
    database = _database(tmp_path)
    request = _request(_manifest(("s3-bucket", "uploads")))
    executor = _open(database)
    executor.provision(request)
    before = database.read_bytes()
    modified = database.stat().st_mtime_ns

    executor.list_tagged_resources(request.tenant_id)
    executor.tenant_role_exists(request.tenant_id)

    names = {path.name for path in tmp_path.iterdir()}
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == modified
    assert f"{_DATABASE_NAME}-journal" not in names
    assert f"{_DATABASE_NAME}-wal" not in names
    assert f"{_DATABASE_NAME}-shm" not in names


def test_destroy_leaves_no_rows_for_the_tenant(tmp_path: Path) -> None:
    database = _database(tmp_path)
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = _open(database)
    executor.provision(request)
    executor.put_bucket_object(request.tenant_id, "uploads", "notes.txt")

    executor.destroy(request.tenant_id)

    assert _count_rows(database, "resources", "tenant_id", request.tenant_id) == 0
    assert _count_rows(database, "object_keys", "tenant_id", request.tenant_id) == 0
    assert _count_rows(database, "tenant_roles", "tenant_id", request.tenant_id) == 0


def test_removing_a_component_leaves_no_rows_for_it(tmp_path: Path) -> None:
    database = _database(tmp_path)
    tenant_id = "tenant-a"
    executor = _open(database)
    executor.provision(
        _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    )
    executor.put_bucket_object(tenant_id, "uploads", "notes.txt")

    executor.provision(_request(_manifest(("sqs-queue", "jobs"))))

    assert _count_rows(database, "resources", "component", "uploads") == 0
    assert _count_rows(database, "object_keys", "component", "uploads") == 0
    assert _count_rows(database, "tenant_roles", "tenant_id", tenant_id) == 1
    assert _count_rows(database, "resources", "component", "jobs") == 1


def test_second_instance_sees_provisioned_resources(tmp_path: Path) -> None:
    database = _database(tmp_path)
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    _open(database).provision(request)

    seen = _open(database).list_tagged_resources(request.tenant_id)

    assert {resource.tags["component"] for resource in seen} == {"uploads", "jobs"}
    assert _open(database).tenant_role_exists(request.tenant_id) is True


def test_destroy_on_a_second_instance_leaves_nothing_for_a_third(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    _open(database).provision(request)

    _open(database).destroy(request.tenant_id)

    third = _open(database)
    assert third.list_tagged_resources(request.tenant_id) == ()
    assert third.tenant_role_exists(request.tenant_id) is False


def test_tenants_stay_isolated_across_instances(tmp_path: Path) -> None:
    database = _database(tmp_path)
    manifest = _manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs"))
    _open(database).provision(_request(manifest, tenant_id="tenant-a", owner="alice"))
    _open(database).provision(_request(manifest, tenant_id="tenant-b", owner="bob"))

    _open(database).destroy("tenant-a")

    remaining = _open(database).list_tagged_resources("tenant-b")
    assert _open(database).list_tagged_resources("tenant-a") == ()
    assert _open(database).tenant_role_exists("tenant-a") is False
    assert {resource.tags["component"] for resource in remaining} == {"uploads", "jobs"}
    assert {resource.tags["owner"] for resource in remaining} == {"bob"}
    assert _open(database).tenant_role_exists("tenant-b") is True


def test_failed_provision_leaves_no_partial_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = _open(database)
    executor.fail_next_provision_atomically()

    with pytest.raises(ExecutorError):
        executor.provision(request)

    fresh = _open(database)
    assert database.is_file()
    assert database.stat().st_size == 0
    assert fresh.list_tagged_resources(request.tenant_id) == ()
    assert fresh.tenant_role_exists(request.tenant_id) is False
    assert set(_TABLES).isdisjoint(_user_tables(database))


def test_later_provision_replaces_the_stored_manifest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _open(database).provision(
        _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    )

    _open(database).provision(_request(_manifest(("sqs-queue", "reports"))))

    seen = _open(database).list_tagged_resources("tenant-a")
    assert {resource.tags["component"] for resource in seen} == {"reports"}
    assert [resource.resource_type for resource in seen] == ["sqs"]
    assert _open(database).tenant_role_exists("tenant-a") is True


def test_concurrent_provisions_leave_one_complete_manifest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    manifests = (
        _manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")),
        _manifest(("sqs-queue", "reports")),
    )
    barrier = threading.Barrier(2)

    def provision(manifest_index: int) -> None:
        barrier.wait(timeout=10)
        _open(database).provision(_request(manifests[manifest_index]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(provision, index) for index in (0, 1)]
        for future in futures:
            future.result(timeout=30)

    names = {
        resource.tags["component"]
        for resource in _open(database).list_tagged_resources("tenant-a")
    }
    assert names in ({"uploads", "jobs"}, {"reports"})
    assert _open(database).tenant_role_exists("tenant-a") is True
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    assert row == ("ok",)


def test_executor_path_with_space_hash_and_percent_works(tmp_path: Path) -> None:
    directory = tmp_path / "state # %"
    directory.mkdir()
    database = directory / _DATABASE_NAME
    request = _request(_manifest(("s3-bucket", "uploads")))

    _open(database).provision(request)

    seen = _open(database).list_tagged_resources(request.tenant_id)
    assert [resource.tags["component"] for resource in seen] == ["uploads"]


def test_build_executor_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from self_service_platform.wiring import build_executor

    home = tmp_path / "absent"
    monkeypatch.setenv("SSP_HOME", str(home))
    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    executor = build_executor()

    assert type(executor).__name__ == "LocalExecutor"
    assert sorted(path.as_posix() for path in tmp_path.rglob("*")) == before
    assert not home.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_created_state_directory_is_user_only(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _open(_database(directory)).provision(_request(_manifest(("sqs-queue", "jobs"))))

    assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_created_database_file_is_user_only(tmp_path: Path) -> None:
    database = _database(tmp_path / "state")
    _open(database).provision(_request(_manifest(("sqs-queue", "jobs"))))

    assert database.stat().st_mode & 0o777 == 0o600


def test_corrupted_executor_file_ends_update_failed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(os.urandom(64))
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        catalog = _catalog_seed(sqlite, TenantStatus.ACTIVE)
        service = _service(catalog, sqlite, _open(database), clock)
        plan = service.plan_update(TENANT_ID, _added_manifest())

        with pytest.raises(UpdateFailedError) as caught:
            service.apply_update(plan, _confirmed(), _updater())

        cause = caught.value.__cause__
        assert isinstance(cause, ExecutorError)
        assert isinstance(cause.__cause__, sqlite3.Error | OSError)
        assert sqlite.get(TENANT_ID).status is TenantStatus.UPDATE_FAILED


def test_corrupted_executor_file_ends_destroy_failed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.write_bytes(os.urandom(64))
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        catalog = _catalog_seed(sqlite, TenantStatus.ACTIVE)
        service = _service(catalog, sqlite, _open(database), clock)
        plan = service.plan_destroy(TENANT_ID)

        with pytest.raises(DestroyFailedError) as caught:
            service.apply_destroy(plan, True, _updater())

        cause = caught.value.__cause__
        assert isinstance(cause, ExecutorError)
        assert isinstance(cause.__cause__, sqlite3.Error | OSError)
        assert sqlite.get(TENANT_ID).status is TenantStatus.DESTROY_FAILED


def test_corrupted_file_during_verification_ends_destroy_failed(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    inner = _open(database)
    clock = MutableClock(T0)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        catalog = _catalog_seed(sqlite, TenantStatus.ACTIVE)
        _arm(inner, catalog)
        service = _service(
            catalog,
            sqlite,
            _CorruptAfterDestroy(inner, database),
            clock,
        )
        plan = service.plan_destroy(TENANT_ID)

        with pytest.raises(DestroyFailedError) as caught:
            service.apply_destroy(plan, True, _updater())

        assert isinstance(caught.value.__cause__, ExecutorError)
        assert sqlite.get(TENANT_ID).status is TenantStatus.DESTROY_FAILED

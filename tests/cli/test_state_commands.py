import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import create_app
from self_service_platform.core.manifest import ManifestEntry
from self_service_platform.core.registry import Registry
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    RegistryUnavailableError,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
)
from self_service_platform.registry.sqlite import SqliteRegistry
from self_service_platform.wiring import open_registry, state_directory

runner = CliRunner()
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
_EXPIRES = "2026-10-17T09:00:00+00:00"
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_DATABASE_NAME = "tenants.sqlite"


def _clock() -> datetime:
    return T0


def _initiator() -> Initiator:
    return Initiator(kind=InitiatorKind.CLI, owner="bob")


def _record(
    *,
    tenant_id: str,
    status: TenantStatus = TenantStatus.CREATING,
    tenant_type: TenantType = TenantType.DEV,
    owner: str = "alice",
    manifest: tuple[ManifestEntry, ...] = (
        ManifestEntry(type="s3-bucket", name="uploads", options={}),
    ),
    catalog_version: str = "catalog-1",
    expires_at: datetime | None = T0,
    last_error: str | None = None,
) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        tenant_type=tenant_type,
        owner=owner,
        status=status,
        manifest=manifest,
        catalog_version=catalog_version,
        initiator=_initiator(),
        expires_at=expires_at,
        last_error=last_error,
        updated_at=T0,
    )


def _steps_to(status: TenantStatus) -> tuple[tuple[TenantStatus, TenantStatus], ...]:
    if status is TenantStatus.CREATING:
        return ()
    if status is TenantStatus.ACTIVE:
        return ((TenantStatus.CREATING, TenantStatus.ACTIVE),)
    if status is TenantStatus.DESTROYING:
        return (
            (TenantStatus.CREATING, TenantStatus.ACTIVE),
            (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        )
    raise ValueError(status)


def _store(path: Path, *records: TenantRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteRegistry(path, _clock) as registry:
        for record in records:
            creating = record.model_copy(update={"status": TenantStatus.CREATING})
            registry.create_tenant(creating)
            for expected, new in _steps_to(record.status):
                registry.transition(
                    record.tenant_id,
                    expected,
                    new,
                    TransitionFields(
                        manifest=record.manifest,
                        catalog_version=record.catalog_version,
                        initiator=record.initiator,
                        expires_at=record.expires_at,
                        last_error=record.last_error,
                    ),
                )


def _read_only(path: Path) -> Callable[[], Registry]:
    def open_it() -> Registry:
        return SqliteRegistry.open_read_only(path)

    return open_it


def _app(open_it: Callable[[], Registry]) -> typer.Typer:
    return create_app(build_catalog(), open_registry=open_it)


def _names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def test_empty_registry_prints_no_tenants(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(database)

    result = runner.invoke(_app(_read_only(database)), ["tenants"])

    assert result.exit_code == 0
    assert result.stdout == "no tenants\n"
    assert result.stderr == ""


def test_tenants_are_listed_sorted_by_id(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(
        database,
        _record(
            tenant_id="mmm",
            status=TenantStatus.DESTROYING,
            tenant_type=TenantType.PROD,
            owner="bob",
            expires_at=None,
        ),
        _record(tenant_id="aaa", status=TenantStatus.ACTIVE),
    )

    result = runner.invoke(_app(_read_only(database)), ["tenants"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        f"aaa active dev alice {_EXPIRES}\nmmm destroying prod bob -\n"
    )


def test_status_prints_each_field(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(
        database,
        _record(
            tenant_id="aaa",
            status=TenantStatus.ACTIVE,
            manifest=(
                ManifestEntry(type="s3-bucket", name="uploads", options={}),
                ManifestEntry(type="sqs-queue", name="jobs", options={}),
            ),
        ),
    )

    result = runner.invoke(_app(_read_only(database)), ["status", "aaa"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        "status: active\n"
        "tenant_type: dev\n"
        "owner: alice\n"
        "initiator: cli bob\n"
        "catalog_version: catalog-1\n"
        f"expires_at: {_EXPIRES}\n"
        "last_error: -\n"
        "uploads s3-bucket\n"
        "jobs sqs-queue\n"
    )


def test_unknown_tenant_is_reported_on_stderr(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(database)

    result = runner.invoke(_app(_read_only(database)), ["status", "missing"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "no such tenant: missing\n"


def test_missing_state_directory_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "absent"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SSP_HOME", str(missing))
    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))
    app = _app(open_registry)

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", "aaa"])
    after = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    assert tenants.exit_code == 0
    assert tenants.stdout == "no tenants\n"
    assert tenants.stderr == ""
    assert status.exit_code == 1
    assert status.stdout == ""
    assert status.stderr == "no such tenant: aaa\n"
    assert after == before
    assert not missing.exists()


def test_existing_commands_do_not_open_the_registry() -> None:
    def open_it() -> SqliteRegistry:
        raise AssertionError("registry opened")

    app = _app(open_it)
    manifest = _EXAMPLES / "valid-manifest.yaml"
    updated = _EXAMPLES / "updated-manifest.yaml"

    catalog = runner.invoke(app, ["catalog"])
    validate = runner.invoke(app, ["validate", str(manifest)])
    estimate = runner.invoke(app, ["estimate", str(manifest)])
    diff = runner.invoke(app, ["diff", str(manifest), str(updated)])

    assert catalog.exit_code == 0
    assert validate.exit_code == 0
    assert estimate.exit_code == 0
    assert diff.exit_code == 0


def test_tenant_output_is_pure_ascii(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(database, _record(tenant_id="aaa", status=TenantStatus.ACTIVE))
    app = _app(_read_only(database))

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", "aaa"])

    tenants.stdout.encode("ascii")
    tenants.stderr.encode("ascii")
    status.stdout.encode("ascii")
    status.stderr.encode("ascii")


def test_tenants_and_status_do_not_modify_the_database_file(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(database, _record(tenant_id="aaa", status=TenantStatus.ACTIVE))
    before = database.read_bytes()
    modified = database.stat().st_mtime_ns
    app = _app(_read_only(database))

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", "aaa"])

    assert tenants.exit_code == 0
    assert status.exit_code == 0
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == modified
    names = _names(tmp_path)
    assert f"{_DATABASE_NAME}-journal" not in names
    assert f"{_DATABASE_NAME}-wal" not in names
    assert f"{_DATABASE_NAME}-shm" not in names


def test_unreadable_database_bytes_are_reported(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    database.write_bytes(os.urandom(64))

    result = runner.invoke(_app(_read_only(database)), ["tenants"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"cannot read state: {database}\n"


def test_database_path_that_is_a_directory_is_reported(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    database.mkdir()

    result = runner.invoke(_app(_read_only(database)), ["status", "aaa"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"cannot read state: {database}\n"


def test_stored_control_and_non_ascii_text_is_escaped(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    tenant_id = "a\n\x1b\u00e9"
    _store(
        database,
        _record(
            tenant_id=tenant_id,
            status=TenantStatus.ACTIVE,
            last_error="boom\n\x1b\u00e9",
        ),
    )
    app = _app(_read_only(database))

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", tenant_id])

    assert tenants.exit_code == 0
    assert tenants.stdout == f"a\\n\\x1b\\u00e9 active dev alice {_EXPIRES}\n"
    assert tenants.stdout.encode("ascii")
    assert status.exit_code == 0
    assert status.stdout.splitlines() == [
        "status: active",
        "tenant_type: dev",
        "owner: alice",
        "initiator: cli bob",
        "catalog_version: catalog-1",
        f"expires_at: {_EXPIRES}",
        "last_error: boom\\n\\x1b\\u00e9",
        "uploads s3-bucket",
    ]
    assert status.stdout.encode("ascii")


def test_relative_ssp_home_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SSP_HOME", "relative/ssp")
    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    result = runner.invoke(_app(open_registry), ["tenants"])
    after = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "SSP_HOME must be absolute: relative/ssp\n"
    assert after == before


def test_absolute_ssp_home_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "state"
    _store(home / _DATABASE_NAME, _record(tenant_id="aaa", status=TenantStatus.ACTIVE))
    monkeypatch.setenv("SSP_HOME", str(home))

    result = runner.invoke(_app(open_registry), ["tenants"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == f"aaa active dev alice {_EXPIRES}\n"


def test_unset_ssp_home_uses_the_home_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSP_HOME", raising=False)
    default = Path.home() / ".ssp"
    existed = default.exists()

    assert state_directory() == default
    assert default.exists() is existed


def test_missing_database_stand_in_refuses_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("SSP_HOME", str(home))
    before = sorted(path.as_posix() for path in home.rglob("*"))
    registry = open_registry()

    assert registry.list_tenants() == ()
    with pytest.raises(TenantNotFoundError):
        registry.get("aaa")
    with pytest.raises(RegistryUnavailableError):
        registry.create_tenant(_record(tenant_id="aaa"))
    with pytest.raises(RegistryUnavailableError):
        registry.transition(
            "aaa",
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            TransitionFields(
                manifest=_record(tenant_id="aaa").manifest,
                catalog_version="catalog-1",
                initiator=_initiator(),
                expires_at=T0,
                last_error=None,
            ),
        )

    assert sorted(path.as_posix() for path in home.rglob("*")) == before
    assert not (home / _DATABASE_NAME).exists()


def test_literal_backslash_differs_from_a_newline(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(
        database,
        _record(tenant_id="a\n", status=TenantStatus.ACTIVE),
        _record(tenant_id="a\\n", status=TenantStatus.ACTIVE),
    )

    result = runner.invoke(_app(_read_only(database)), ["tenants"])

    assert result.exit_code == 0
    assert result.stdout == (
        f"a\\n active dev alice {_EXPIRES}\na\\\\n active dev alice {_EXPIRES}\n"
    )


def test_carriage_return_and_tab_are_escaped(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    _store(database, _record(tenant_id="a\r\tb", status=TenantStatus.ACTIVE))

    result = runner.invoke(_app(_read_only(database)), ["tenants"])

    assert result.exit_code == 0
    assert result.stdout == f"a\\r\\tb active dev alice {_EXPIRES}\n"
    assert result.stdout.encode("ascii")


def test_read_only_open_accepts_a_directory_name_with_space_hash_and_percent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state # %" / _DATABASE_NAME
    _store(database, _record(tenant_id="aaa", status=TenantStatus.ACTIVE))

    registry = SqliteRegistry.open_read_only(database)
    try:
        found = registry.list_tenants()
    finally:
        registry.close()

    assert [record.tenant_id for record in found] == ["aaa"]


def test_empty_ssp_home_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SSP_HOME", "")
    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    result = runner.invoke(_app(open_registry), ["tenants"])
    after = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "SSP_HOME must be absolute: \n"
    assert "Traceback" not in result.stderr
    assert after == before


def test_zero_byte_database_is_reported(tmp_path: Path) -> None:
    database = tmp_path / _DATABASE_NAME
    database.write_bytes(b"")
    before = database.read_bytes()
    modified = database.stat().st_mtime_ns
    app = _app(_read_only(database))

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", "aaa"])

    assert tenants.exit_code == 2
    assert tenants.stdout == ""
    assert tenants.stderr == f"cannot read state: {database}\n"
    assert "Traceback" not in tenants.stderr
    assert status.exit_code == 2
    assert status.stdout == ""
    assert status.stderr == f"cannot read state: {database}\n"
    assert "Traceback" not in status.stderr
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == modified


def test_missing_database_file_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("SSP_HOME", str(home))
    before = sorted(path.as_posix() for path in home.rglob("*"))
    app = _app(open_registry)

    tenants = runner.invoke(app, ["tenants"])
    status = runner.invoke(app, ["status", "aaa"])

    assert tenants.exit_code == 0
    assert tenants.stdout == "no tenants\n"
    assert tenants.stderr == ""
    assert status.exit_code == 1
    assert status.stdout == ""
    assert status.stderr == "no such tenant: aaa\n"
    assert sorted(path.as_posix() for path in home.rglob("*")) == before
    assert not (home / _DATABASE_NAME).exists()

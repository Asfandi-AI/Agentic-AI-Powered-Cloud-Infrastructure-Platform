import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from self_service_platform.app import app
from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import _escape_stored, create_app
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.executor import ExecutorError, ResourceRef
from self_service_platform.core.manifest import ManifestEntry
from self_service_platform.core.registry import Clock
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    RegistryUnavailableError,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
)
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.registry.sqlite import SqliteRegistry
from self_service_platform.wiring import open_registry

runner = CliRunner()
T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
_MOMENT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_VALID = _EXAMPLES / "valid-manifest.yaml"
_NAIVE = "clock must return timezone-aware UTC\n"
_MAX = "max must be at least 1\n"
_MANIFEST = (
    ManifestEntry(type="s3-bucket", name="uploads", options={}),
    ManifestEntry(type="sqs-queue", name="jobs", options={}),
)
_CLI = Initiator(kind=InitiatorKind.CLI, owner="alice")
_STEPS: dict[TenantStatus, tuple[tuple[TenantStatus, TenantStatus], ...]] = {
    TenantStatus.CREATING: (),
    TenantStatus.ACTIVE: ((TenantStatus.CREATING, TenantStatus.ACTIVE),),
    TenantStatus.DESTROYED: (
        (TenantStatus.CREATING, TenantStatus.ACTIVE),
        (TenantStatus.ACTIVE, TenantStatus.DESTROYING),
        (TenantStatus.DESTROYING, TenantStatus.DESTROYED),
    ),
}


class _FailDestroy:
    def provision(self, request: object) -> None:
        return None

    def destroy(self, tenant_id: str) -> None:
        raise ExecutorError("injected destroy failure")

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        return ()

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return False


class _Unwritable:
    def __init__(self, inner: SqliteRegistry, path: Path) -> None:
        self._inner = inner
        self._path = path

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._inner.list_tenants()

    def get(self, tenant_id: str) -> TenantRecord:
        raise RegistryUnavailableError(self._path)

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        raise RegistryUnavailableError(self._path)


@pytest.fixture(autouse=True)
def ssp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("SSP_HOME", str(home))
    return home


def _text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _clock(now: datetime) -> Clock:
    return lambda: now


class _Audit:
    def __init__(self) -> None:
        self.entries: list[object] = []

    def write(self, entry: object) -> None:
        self.entries.append(entry)


def _deps(clock: Clock) -> tuple[object, object, object]:
    issuer, verifier = confirmation_pair(clock)
    return _Audit(), issuer, verifier


def _record(
    *,
    tenant_id: str,
    tenant_type: TenantType = TenantType.DEV,
    status: TenantStatus = TenantStatus.ACTIVE,
    expires_at: datetime | None = T0,
    catalog_version: str = "catalog-1",
) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        tenant_type=tenant_type,
        owner="alice",
        status=status,
        manifest=_MANIFEST,
        catalog_version=catalog_version,
        initiator=_CLI,
        expires_at=expires_at,
        last_error=None,
        updated_at=T0,
    )


def _store(home: Path, *records: TenantRecord) -> None:
    home.mkdir(parents=True, exist_ok=True)
    registry = SqliteRegistry(home / "tenants.sqlite", _clock(T0))
    try:
        for record in records:
            registry.create_tenant(
                record.model_copy(update={"status": TenantStatus.CREATING})
            )
            current = record
            for expected, new in _STEPS[record.status]:
                current = registry.transition(
                    record.tenant_id,
                    expected,
                    new,
                    TransitionFields(
                        manifest=record.manifest,
                        catalog_version=record.catalog_version,
                        initiator=record.initiator,
                        expires_at=record.expires_at,
                        last_error=None,
                    ),
                )
            assert current.status is record.status
    finally:
        registry.close()


def _load(home: Path, tenant_id: str) -> TenantRecord:
    registry = SqliteRegistry.open_read_only(home / "tenants.sqlite")
    try:
        return registry.get(tenant_id)
    finally:
        registry.close()


def _names(home: Path) -> list[str]:
    if not home.exists():
        return []
    return sorted(path.name for path in home.rglob("*") if path.is_file())


def _closed_service() -> PlatformService:
    raise AssertionError("service opened")


def _application(
    clock: Clock | None = None,
    open_service: Callable[[], PlatformService] | None = None,
) -> object:
    return create_app(
        build_catalog(),
        open_registry=open_registry,
        open_service=_closed_service if open_service is None else open_service,
        clock=clock,
    )


@contextmanager
def _real_service(
    home: Path,
    clock: Clock,
    executor: object | None = None,
) -> Iterator[PlatformService]:
    registry = SqliteRegistry(home / "tenants.sqlite", clock)
    try:
        audit, issuer, verifier = _deps(clock)
        yield PlatformService(
            catalog=build_catalog(),
            registry=registry,
            executor=FakeExecutor() if executor is None else executor,
            clock=clock,
            ttl_policy=TtlPolicy(dev=timedelta(days=1), ephemeral=timedelta(hours=3)),
            audit_log=audit,
            issuer=issuer,
            verifier=verifier,
        )
    finally:
        registry.close()


def _refuse_input(*_args: object, **_kwargs: object) -> str:
    raise AssertionError("stdin read")


def _guard_service(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []

    def guard(self: PlatformService, *_args: object, **_kwargs: object) -> None:
        opened.append("opened")
        raise AssertionError("service opened")

    monkeypatch.setattr(PlatformService, "__init__", guard)
    return opened


def _invoke(application: object, *args: str, stdin: str = "") -> Result:
    return runner.invoke(application, list(args), input=stdin)


def test_sweep_without_yes_is_a_dry_run(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(ssp_home, _record(tenant_id="demo"))
    before = _load(ssp_home, "demo")
    monkeypatch.setattr(
        "self_service_platform.cli.main._stdin_is_terminal", lambda: True
    )
    monkeypatch.setattr("builtins.input", _refuse_input)
    files = _names(ssp_home)

    result = _invoke(_application(_clock(T0)), "sweep", stdin="y\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    assert result.stdout == _text(
        "skipped: demo dry run",
        "summary: 0 destroyed, 0 failed, 1 skipped",
    )
    assert result.stdout.isascii()
    assert _load(ssp_home, "demo") == before
    assert _names(ssp_home) == files


def test_sweep_yes_does_not_read_stdin(
    ssp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(ssp_home, _record(tenant_id="demo"))
    monkeypatch.setattr(
        "self_service_platform.cli.main._stdin_is_terminal", lambda: True
    )
    monkeypatch.setattr("builtins.input", _refuse_input)
    with _real_service(ssp_home, _clock(T0)) as service:
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
            stdin="n\n",
        )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _text(
        "destroyed: demo",
        "summary: 1 destroyed, 0 failed, 0 skipped",
    )
    assert _load(ssp_home, "demo").status is TenantStatus.DESTROYED
    assert _load(ssp_home, "demo").initiator.kind is InitiatorKind.SWEEPER


def test_sweep_yes_exits_1_when_an_entry_fails(ssp_home: Path) -> None:
    _store(ssp_home, _record(tenant_id="demo"))
    with _real_service(ssp_home, _clock(T0), _FailDestroy()) as service:
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
        )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    assert result.stdout == _text(
        "failed: demo injected destroy failure",
        "summary: 0 destroyed, 1 failed, 0 skipped",
    )
    assert _load(ssp_home, "demo").status is TenantStatus.DESTROY_FAILED


def test_sweep_yes_exits_0_when_every_entry_is_skipped(ssp_home: Path) -> None:
    _store(ssp_home, _record(tenant_id="demo", status=TenantStatus.CREATING))
    before = _load(ssp_home, "demo")
    with _real_service(ssp_home, _clock(T0)) as service:
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
        )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _text(
        "skipped: demo creating",
        "summary: 0 destroyed, 0 failed, 1 skipped",
    )
    assert _load(ssp_home, "demo") == before


def test_sweep_exits_2_when_the_registry_cannot_be_read(ssp_home: Path) -> None:
    database = ssp_home / "tenants.sqlite"
    database.mkdir(parents=True)
    before = _names(ssp_home)

    dry_run = runner.invoke(app, ["sweep"])
    real = runner.invoke(app, ["sweep", "--yes"])

    message = f"cannot read state: {database}\n"
    assert dry_run.exit_code == 2
    assert dry_run.stdout == ""
    assert dry_run.stderr == message
    assert "Traceback" not in dry_run.stderr
    assert real.exit_code == 2
    assert real.stdout == ""
    assert real.stderr == message
    assert "Traceback" not in real.stderr
    assert _names(ssp_home) == before


def test_sweep_yes_exits_2_when_the_state_cannot_be_written(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    _store(ssp_home, _record(tenant_id="demo"))
    before = _load(ssp_home, "demo")
    blocked = tmp_path / "blocked"
    registry = SqliteRegistry(ssp_home / "tenants.sqlite", _clock(T0))
    try:
        audit, issuer, verifier = _deps(_clock(T0))
        service = PlatformService(
            catalog=build_catalog(),
            registry=_Unwritable(registry, blocked),
            executor=FakeExecutor(),
            clock=_clock(T0),
            ttl_policy=TtlPolicy(dev=timedelta(days=1), ephemeral=timedelta(hours=3)),
            audit_log=audit,
            issuer=issuer,
            verifier=verifier,
        )
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
        )
    finally:
        registry.close()

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"cannot write state: {blocked}\n"
    assert "Traceback" not in result.stderr
    assert _load(ssp_home, "demo") == before


def test_sweep_output_is_ascii_and_escapes_stored_text(ssp_home: Path) -> None:
    tenant_id = "up\nloads\x1b"
    _store(ssp_home, _record(tenant_id=tenant_id))

    result = _invoke(_application(_clock(T0)), "sweep")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _text(
        f"skipped: {_escape_stored(tenant_id)} dry run",
        "summary: 0 destroyed, 0 failed, 1 skipped",
    )
    assert result.stdout.isascii()
    assert "\nloads" not in result.stdout


def test_sweep_yes_stops_after_max(ssp_home: Path) -> None:
    _store(
        ssp_home,
        _record(tenant_id="early"),
        _record(tenant_id="later"),
    )
    with _real_service(ssp_home, _clock(T0)) as service:
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
            "--max",
            "1",
        )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _text(
        "destroyed: early",
        "skipped: later limit reached",
        "summary: 1 destroyed, 0 failed, 1 skipped",
    )
    assert _load(ssp_home, "early").status is TenantStatus.DESTROYED
    assert _load(ssp_home, "later").status is TenantStatus.ACTIVE


def test_sweep_yes_defaults_to_ten_destroys(ssp_home: Path) -> None:
    names = [f"n{index:02d}" for index in range(1, 12)]
    _store(ssp_home, *(_record(tenant_id=name) for name in names))
    with _real_service(ssp_home, _clock(T0)) as service:
        result = _invoke(
            _application(_clock(T0), open_service=lambda: service),
            "sweep",
            "--yes",
        )

    lines = [f"destroyed: {name}" for name in names[:10]]
    lines.append("skipped: n11 limit reached")
    lines.append("summary: 10 destroyed, 0 failed, 1 skipped")
    assert result.exit_code == 0
    assert result.stdout == _text(*lines)
    assert _load(ssp_home, "n11").status is TenantStatus.ACTIVE
    assert _load(ssp_home, "n01").status is TenantStatus.DESTROYED


@pytest.mark.parametrize("value", ["0", "-1"])
def test_sweep_max_below_one_is_a_usage_error(ssp_home: Path, value: str) -> None:
    assert not ssp_home.exists()

    result = runner.invoke(app, ["sweep", "--max", value])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == _MAX
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_sweep_dry_run_on_a_missing_directory_creates_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _guard_service(monkeypatch)
    assert not ssp_home.exists()

    result = runner.invoke(app, ["sweep"])

    assert opened == []
    assert result.exit_code == 0
    assert result.stdout == "nothing to sweep\n"
    assert result.stderr == ""
    assert not ssp_home.exists()


def test_sweep_yes_on_a_missing_directory_creates_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _guard_service(monkeypatch)
    assert not ssp_home.exists()
    before = _names(ssp_home)

    result = runner.invoke(app, ["sweep", "--yes"])

    assert opened == []
    assert result.exit_code == 0
    assert result.stdout == "nothing to sweep\n"
    assert result.stderr == ""
    assert not ssp_home.exists()
    assert _names(ssp_home) == before


def test_sweep_yes_on_a_missing_database_creates_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssp_home.mkdir()
    before = _names(ssp_home)
    opened = _guard_service(monkeypatch)

    result = runner.invoke(app, ["sweep", "--yes"])

    assert opened == []
    assert result.exit_code == 0
    assert result.stdout == "nothing to sweep\n"
    assert result.stderr == ""
    assert _names(ssp_home) == before
    assert before == []


def test_sweep_yes_does_not_open_the_service_when_only_a_prod_tenant_is_expired(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(
        ssp_home,
        _record(
            tenant_id="prod-one",
            tenant_type=TenantType.PROD,
            expires_at=T0 - timedelta(days=1),
        ),
    )
    before = _load(ssp_home, "prod-one")
    files = _names(ssp_home)
    opened = _guard_service(monkeypatch)

    result = runner.invoke(app, ["sweep", "--yes"])

    assert opened == []
    assert result.exit_code == 0
    assert result.stdout == "nothing to sweep\n"
    assert result.stderr == ""
    assert _load(ssp_home, "prod-one") == before
    assert _names(ssp_home) == files
    assert "local-executor.sqlite" not in files


def test_create_app_default_clock_is_timezone_aware_utc(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default clock must call datetime.now(UTC) in cli.main when the command runs.
    seen: list[datetime] = []

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            if tz is not UTC:
                raise AssertionError(tz)
            seen.append(_MOMENT)
            return _MOMENT

    monkeypatch.setattr("self_service_platform.cli.main.datetime", _Frozen)
    _store(
        ssp_home,
        _record(tenant_id="due", expires_at=_MOMENT),
        _record(tenant_id="later", expires_at=_MOMENT + timedelta(seconds=1)),
    )

    result = _invoke(_application(), "sweep")

    assert seen
    assert all(moment == _MOMENT and moment.tzinfo is UTC for moment in seen)
    assert seen[0].utcoffset() == timedelta(0)
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _text(
        "skipped: due dry run",
        "summary: 0 destroyed, 0 failed, 1 skipped",
    )


def test_sweep_rejects_a_naive_clock(ssp_home: Path) -> None:
    _store(ssp_home, _record(tenant_id="demo"))
    before = _load(ssp_home, "demo")

    def naive() -> datetime:
        return datetime(2026, 10, 26, 12, 0)

    with _real_service(ssp_home, _clock(T0)) as service:
        result = _invoke(
            _application(naive, open_service=lambda: service),
            "sweep",
            "--yes",
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _NAIVE
    assert "Traceback" not in result.stderr
    assert _load(ssp_home, "demo") == before


def test_subprocess_sweep_destroys_an_expired_ephemeral_tenant(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    executable = shutil.which("ssp")
    assert executable is not None
    env = os.environ.copy()
    env["SSP_HOME"] = str(ssp_home)

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

    created = run(
        [
            "create",
            "demo",
            str(_VALID),
            "--type",
            "ephemeral",
            "--owner",
            "alice",
            "--yes",
        ]
    )
    database = ssp_home / "tenants.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE tenants SET expires_at = ? WHERE tenant_id = ?",
            ("2000-01-01T00:00:00+00:00", "demo"),
        )
        connection.commit()
    finally:
        connection.close()

    dry_run = run(["sweep"])
    real = run(["sweep", "--yes"])
    executor_file = ssp_home / "local-executor.sqlite"
    resources = sqlite3.connect(executor_file)
    try:
        tagged = resources.execute(
            "SELECT COUNT(*) FROM resources WHERE tag_tenant_id = ?",
            ("demo",),
        ).fetchone()
        roles = resources.execute(
            "SELECT COUNT(*) FROM tenant_roles WHERE tenant_id = ?",
            ("demo",),
        ).fetchone()
    finally:
        resources.close()
    record = _load(ssp_home, "demo")

    assert created.returncode == 0
    assert "Traceback" not in created.stderr
    assert dry_run.returncode == 0
    assert dry_run.stderr == ""
    assert "Traceback" not in dry_run.stderr
    assert dry_run.stdout == _text(
        "skipped: demo dry run",
        "summary: 0 destroyed, 0 failed, 1 skipped",
    )
    assert real.returncode == 0
    assert real.stderr == ""
    assert "Traceback" not in real.stderr
    assert real.stdout == _text(
        "destroyed: demo",
        "summary: 1 destroyed, 0 failed, 0 skipped",
    )
    assert tagged == (0,)
    assert roles == (0,)
    assert record.status is TenantStatus.DESTROYED
    assert record.initiator == Initiator(kind=InitiatorKind.SWEEPER, owner="sweeper")


def test_concurrent_sweeps_leave_one_destroyed_tenant(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    # Proves no corruption, not that a race necessarily happened.
    executable = shutil.which("ssp")
    assert executable is not None
    env = os.environ.copy()
    env["SSP_HOME"] = str(ssp_home)
    created = subprocess.run(
        [
            executable,
            "create",
            "demo",
            str(_VALID),
            "--type",
            "ephemeral",
            "--owner",
            "alice",
            "--yes",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    connection = sqlite3.connect(ssp_home / "tenants.sqlite")
    try:
        connection.execute(
            "UPDATE tenants SET expires_at = ? WHERE tenant_id = ?",
            ("2000-01-01T00:00:00+00:00", "demo"),
        )
        connection.commit()
    finally:
        connection.close()

    def spawn() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [executable, "sweep", "--yes"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    first = spawn()
    second = spawn()
    _out_first, err_first = first.communicate(timeout=60)
    _out_second, err_second = second.communicate(timeout=60)
    record = _load(ssp_home, "demo")

    assert created.returncode == 0
    assert "Traceback" not in err_first
    assert "Traceback" not in err_second
    assert record.status is TenantStatus.DESTROYED
    _integrity_ok(ssp_home / "tenants.sqlite")
    _integrity_ok(ssp_home / "local-executor.sqlite")


def _integrity_ok(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    assert row == ("ok",)

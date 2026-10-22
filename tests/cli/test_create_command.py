import getpass
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from self_service_platform.app import app
from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import _escape_stored, create_app
from self_service_platform.core.manifest import load_manifest_from_file
from self_service_platform.core.registry import Registry
from self_service_platform.core.service import PlatformService
from self_service_platform.core.tenant import TenantRecord, TenantStatus
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

runner = CliRunner()
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_VALID = _EXAMPLES / "valid-manifest.yaml"
_INVALID = _EXAMPLES / "invalid-manifest.yaml"
_DEV_TTL = timedelta(days=1)
_EPHEMERAL_TTL = timedelta(hours=3)
_ESTIMATE_LINES = (
    "uploads s3-bucket",
    "  S3 storage not priced",
    "  total: not priced",
    "jobs sqs-queue",
    "  SQS requests not priced",
    "  total: not priced",
    "manifest total: not priced",
    "POC heuristic",
)
_INVALID_ISSUES = (
    "position 1 uploads: unknown component type no-such-type\n"
    "position 2 jobs: unknown option foo\n"
)
_PROMPT = "Create this tenant? [y/N]"
_PLAN_ONLY = "plan only: nothing was written"
_USER_UNKNOWN = "cannot determine the operating system user\n"
_SECOND_PROCESS = """\
import sys
from self_service_platform.app import app

def run(args: list[str]) -> None:
    try:
        app(args)
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise

run(["tenants"])
run(["status", "demo"])
"""


@pytest.fixture(autouse=True)
def ssp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("SSP_HOME", str(home))
    return home


def _command(
    manifest: Path,
    *options: str,
    tenant_id: str = "demo",
    owner: str = "alice",
    tenant_type: str = "dev",
) -> list[str]:
    return [
        "create",
        tenant_id,
        str(manifest),
        "--type",
        tenant_type,
        "--owner",
        owner,
        *options,
    ]


def _terminal(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(
        "self_service_platform.cli.main._stdin_is_terminal",
        lambda: value,
    )


def _patch_getuser(monkeypatch: pytest.MonkeyPatch, getuser: Callable[[], str]) -> None:
    monkeypatch.setattr(getpass, "getuser", getuser)
    module = sys.modules["self_service_platform.cli.main"]
    if hasattr(module, "getuser"):
        monkeypatch.setattr(module, "getuser", getuser)


def _entries(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_expires(line: str, start: datetime, end: datetime, ttl: timedelta) -> str:
    prefix = "expires_at: "
    assert line.startswith(prefix)
    text = line.removeprefix(prefix)
    parsed = datetime.fromisoformat(text)
    assert parsed.tzinfo is not None
    assert start + ttl <= parsed <= end + ttl
    return text


def _tenant(home: Path, tenant_id: str) -> TenantRecord:
    registry = SqliteRegistry.open_read_only(home / "tenants.sqlite")
    try:
        return registry.get(tenant_id)
    finally:
        registry.close()


def _integrity_ok(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    assert row == ("ok",)


def _start_create(
    home: Path,
    tenant_id: str,
    manifest: Path,
    cwd: Path,
) -> subprocess.Popen[str]:
    executable = shutil.which("ssp")
    assert executable is not None
    env = os.environ.copy()
    env["SSP_HOME"] = str(home)
    return subprocess.Popen(
        [
            executable,
            "create",
            tenant_id,
            str(manifest),
            "--type",
            "dev",
            "--owner",
            "alice",
            "--yes",
        ],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _created_lines(expires: str) -> list[str]:
    user = _escape_stored(getpass.getuser())
    version = build_catalog().catalog_version
    return [
        "created: demo status active",
        "status: active",
        "tenant_type: dev",
        "owner: alice",
        f"initiator: cli {user}",
        f"catalog_version: {version}",
        f"expires_at: {expires}",
        "last_error: -",
        "uploads s3-bucket",
        "jobs sqs-queue",
    ]


def test_plan_only_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)
    before = _entries(tmp_path)

    result = runner.invoke(app, _command(_VALID, "--plan-only"))

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.splitlines()[-1] == _PLAN_ONLY
    assert _PROMPT not in result.stdout
    assert _entries(tmp_path) == before
    assert not ssp_home.exists()


def test_plan_only_with_yes_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)
    before = _entries(tmp_path)

    result = runner.invoke(
        app,
        _command(_VALID, "--plan-only", "--yes"),
        input="n\n",
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.splitlines()[-1] == _PLAN_ONLY
    assert _PROMPT not in result.stdout
    assert "created:" not in result.stdout
    assert _entries(tmp_path) == before
    assert not ssp_home.exists()


def test_dev_plan_shows_the_estimate_and_expiry(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)
    start = datetime.now(UTC)
    result = runner.invoke(app, _command(_VALID, "--plan-only"))
    end = datetime.now(UTC)

    assert result.exit_code == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[:3] == [
        "tenant_id: demo",
        "tenant_type: dev",
        "owner: alice",
    ]
    assert tuple(lines[3:11]) == _ESTIMATE_LINES
    _assert_expires(lines[11], start, end, _DEV_TTL)
    assert lines[12:] == [_PLAN_ONLY]
    assert not ssp_home.exists()


def test_prod_plan_expires_at_is_a_dash(ssp_home: Path) -> None:
    result = runner.invoke(
        app,
        _command(_VALID, "--plan-only", tenant_type="prod"),
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "expires_at: -" in result.stdout.splitlines()
    assert not ssp_home.exists()


def test_ephemeral_plan_expires_in_three_hours(ssp_home: Path) -> None:
    start = datetime.now(UTC)
    result = runner.invoke(
        app,
        _command(_VALID, "--plan-only", tenant_type="ephemeral"),
    )
    end = datetime.now(UTC)

    assert result.exit_code == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert "tenant_type: ephemeral" in lines
    _assert_expires(lines[-2], start, end, _EPHEMERAL_TTL)
    assert not ssp_home.exists()


def test_missing_terminal_refuses_without_writing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, False)

    result = runner.invoke(app, _command(_VALID), input="y\n")

    assert result.exit_code == 1
    assert result.stderr == "confirmation required; pass --yes\n"
    assert "Traceback" not in result.stderr
    assert result.stdout.splitlines()[:3] == [
        "tenant_id: demo",
        "tenant_type: dev",
        "owner: alice",
    ]
    assert _PROMPT not in result.stdout
    assert "created:" not in result.stdout
    assert _PLAN_ONLY not in result.stdout.splitlines()
    assert not ssp_home.exists()


@pytest.mark.parametrize("answer", ["y", "yes", "Y", "YES"])
def test_affirmative_answer_creates(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _command(_VALID), input=f"{answer}\n")
    status = runner.invoke(app, ["status", "demo"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _PROMPT in result.stdout.splitlines()
    assert "created: demo status active" in result.stdout.splitlines()
    assert status.exit_code == 0
    assert status.stdout.splitlines()[0] == "status: active"
    assert (ssp_home / "tenants.sqlite").is_file()


def test_no_answer_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _command(_VALID), input="n\n")

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines()[-1] == _PROMPT
    assert "created:" not in result.stdout
    assert not ssp_home.exists()


def test_empty_answer_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _command(_VALID), input="\n")

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines()[-1] == _PROMPT
    assert "created:" not in result.stdout
    assert not ssp_home.exists()


def test_end_of_file_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _command(_VALID), input="")

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines()[-1] == _PROMPT
    assert "created:" not in result.stdout
    assert not ssp_home.exists()


def test_yes_flag_creates(ssp_home: Path) -> None:
    start = datetime.now(UTC)
    result = runner.invoke(app, _command(_VALID, "--yes"))
    end = datetime.now(UTC)

    assert result.exit_code == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    expires = _assert_expires(lines[11], start, end, _DEV_TTL)
    assert lines[:3] == [
        "tenant_id: demo",
        "tenant_type: dev",
        "owner: alice",
    ]
    assert tuple(lines[3:11]) == _ESTIMATE_LINES
    assert lines[12:] == _created_lines(expires)
    assert (ssp_home / "tenants.sqlite").is_file()


def test_duplicate_tenant_leaves_the_first_record_unchanged(ssp_home: Path) -> None:
    first = runner.invoke(app, _command(_VALID, "--yes"))
    assert first.exit_code == 0
    status = runner.invoke(app, ["status", "demo"])
    snapshot = _file_bytes(ssp_home)

    second = runner.invoke(app, _command(_VALID, "--yes"))
    again = runner.invoke(app, ["status", "demo"])

    assert second.exit_code == 1
    assert second.stderr == "tenant already exists: demo\n"
    assert "Traceback" not in second.stderr
    assert _file_bytes(ssp_home) == snapshot
    assert again.stdout == status.stdout
    assert again.exit_code == 0


def test_invalid_manifest_writes_nothing(ssp_home: Path) -> None:
    result = runner.invoke(app, _command(_INVALID, "--yes"))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _INVALID_ISSUES
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


@pytest.mark.parametrize(
    ("tenant_id", "escaped"),
    [
        ("", ""),
        ("   ", "   "),
        ("a\nb", "a\\nb"),
        ("\t", "\\t"),
    ],
)
def test_invalid_tenant_id_is_rejected(
    tmp_path: Path,
    ssp_home: Path,
    tenant_id: str,
    escaped: str,
) -> None:
    missing = tmp_path / "missing.yaml"

    result = runner.invoke(
        app,
        _command(missing, "--yes", tenant_id=tenant_id),
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"invalid tenant id: {escaped}\n"
    assert "Traceback" not in result.stderr
    assert not missing.exists()
    assert not ssp_home.exists()


@pytest.mark.parametrize(
    ("owner", "escaped"),
    [
        ("", ""),
        ("   ", "   "),
        ("o\nwn", "o\\nwn"),
        ("\t", "\\t"),
    ],
)
def test_invalid_owner_is_rejected(
    tmp_path: Path,
    ssp_home: Path,
    owner: str,
    escaped: str,
) -> None:
    missing = tmp_path / "missing.yaml"

    result = runner.invoke(app, _command(missing, "--yes", owner=owner))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"invalid owner: {escaped}\n"
    assert "Traceback" not in result.stderr
    assert not missing.exists()
    assert not ssp_home.exists()


def test_getuser_failure_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> str:
        raise OSError("no user")

    _patch_getuser(monkeypatch, broken)

    result = runner.invoke(app, _command(_VALID, "--yes"))

    assert result.exit_code == 2
    assert result.stderr == _USER_UNKNOWN
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_operating_system_user_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
) -> None:
    def blank_user() -> str:
        return blank

    _patch_getuser(monkeypatch, blank_user)

    result = runner.invoke(app, _command(_VALID, "--yes"))

    assert result.exit_code == 2
    assert result.stderr == _USER_UNKNOWN
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_ssp_home_parent_file_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"file")
    home = blocked / "state"
    database = home / "tenants.sqlite"
    monkeypatch.setenv("SSP_HOME", str(home))

    result = runner.invoke(app, _command(_VALID, "--yes"))

    assert result.exit_code == 2
    assert result.stdout != ""
    assert "created:" not in result.stdout
    assert result.stderr == f"cannot write state: {database}\n"
    assert "Traceback" not in result.stderr
    assert blocked.read_bytes() == b"file"
    assert not home.exists()


def test_database_path_that_is_a_directory_cannot_be_written(ssp_home: Path) -> None:
    database = ssp_home / "tenants.sqlite"
    database.mkdir(parents=True)

    result = runner.invoke(app, _command(_VALID, "--yes"))

    assert result.exit_code == 2
    assert "created:" not in result.stdout
    assert result.stderr == f"cannot write state: {database}\n"
    assert "Traceback" not in result.stderr
    assert database.is_dir()
    assert not (ssp_home / "local-executor.sqlite").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="a read-only directory does not block creation on Windows",
)
def test_read_only_ssp_home_parent_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    home = parent / "state"
    database = home / "tenants.sqlite"
    monkeypatch.setenv("SSP_HOME", str(home))
    try:
        result = runner.invoke(app, _command(_VALID, "--yes"))
    finally:
        parent.chmod(0o700)

    assert result.exit_code == 2
    assert result.stderr == f"cannot write state: {database}\n"
    assert "Traceback" not in result.stderr
    assert not home.exists()


def test_provision_failure_leaves_the_tenant_creating(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = LocalExecutor.__init__

    def armed(self: LocalExecutor, path: Path) -> None:
        original(self, path)
        self.fail_next_provision()

    monkeypatch.setattr(LocalExecutor, "__init__", armed)

    result = runner.invoke(app, _command(_VALID, "--yes"))
    status = runner.invoke(app, ["status", "demo"])

    assert result.exit_code == 1
    assert result.stderr == (
        "injected provision failure\n"
        "tenant is left in status creating; see ssp status demo\n"
    )
    assert "Traceback" not in result.stderr
    assert "created:" not in result.stdout
    assert status.exit_code == 0
    assert status.stdout.splitlines()[0] == "status: creating"
    assert "last_error: -" in status.stdout.splitlines()
    assert (ssp_home / "tenants.sqlite").is_file()


def test_create_output_is_pure_ascii(ssp_home: Path) -> None:
    result = runner.invoke(
        app,
        _command(_VALID, "--yes", tenant_id="d\u00e9mo", owner="caf\u00e9"),
    )
    status = runner.invoke(app, ["status", "d\u00e9mo"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert "tenant_id: d\\u00e9mo" in result.stdout.splitlines()
    assert "owner: caf\\u00e9" in result.stdout.splitlines()
    assert "created: d\\u00e9mo status active" in result.stdout.splitlines()
    assert status.exit_code == 0
    assert status.stdout.isascii()
    assert status.stderr == ""
    assert (ssp_home / "tenants.sqlite").is_file()


def test_create_persists_for_a_second_process(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
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
            "dev",
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
    followed = subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    user = _escape_stored(getpass.getuser())
    version = build_catalog().catalog_version
    sqlite_files = list(tmp_path.rglob("*.sqlite"))

    assert created.returncode == 0
    assert "created: demo status active" in created.stdout.splitlines()
    assert "Traceback" not in created.stderr
    assert followed.returncode == 0
    assert "Traceback" not in followed.stderr
    followed_lines = followed.stdout.splitlines()
    assert any(line.startswith("demo active dev alice ") for line in followed_lines)
    assert "status: active" in followed_lines
    assert f"initiator: cli {user}" in followed_lines
    assert f"catalog_version: {version}" in followed_lines
    assert sqlite_files
    assert all(ssp_home in path.parents for path in sqlite_files)


def test_existing_commands_do_not_open_the_service(tmp_path: Path) -> None:
    database = tmp_path / "tenants.sqlite"
    SqliteRegistry(database, lambda: datetime.now(UTC)).close()

    def open_registry() -> Registry:
        return SqliteRegistry.open_read_only(database)

    def open_service() -> PlatformService:
        raise AssertionError("service opened")

    cli = create_app(
        build_catalog(),
        open_registry=open_registry,
        open_service=open_service,
    )
    catalog = runner.invoke(cli, ["catalog"])
    validate = runner.invoke(cli, ["validate", str(_VALID)])
    estimate = runner.invoke(cli, ["estimate", str(_VALID)])
    diff = runner.invoke(
        cli,
        ["diff", str(_VALID), str(_EXAMPLES / "updated-manifest.yaml")],
    )
    tenants = runner.invoke(cli, ["tenants"])
    status = runner.invoke(cli, ["status", "missing"])

    assert catalog.exit_code == 0
    assert validate.exit_code == 0
    assert estimate.exit_code == 0
    assert diff.exit_code == 0
    assert tenants.exit_code == 0
    assert tenants.stdout == "no tenants\n"
    assert status.exit_code == 1
    assert status.stderr == "no such tenant: missing\n"


def test_affirmative_answer_stores_the_printed_plan(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _command(_VALID), input="y\n")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[12] == _PROMPT
    expires = lines[11].removeprefix("expires_at: ")
    version = next(
        line.removeprefix("catalog_version: ")
        for line in lines
        if line.startswith("catalog_version: ")
    )
    record = _tenant(ssp_home, "demo")
    stored = [f"{entry.name} {entry.type}" for entry in record.manifest]
    assert stored == [lines[3], lines[6]]
    assert record.catalog_version == version
    assert f"owner: {record.owner}" == lines[2]
    assert f"tenant_type: {record.tenant_type.value}" == lines[1]
    assert record.expires_at is not None
    assert f"expires_at: {record.expires_at.isoformat()}" == lines[11]
    assert expires == record.expires_at.isoformat()
    assert record.status is TenantStatus.ACTIVE


def test_corrupted_executor_file_leaves_the_tenant_creating(ssp_home: Path) -> None:
    ssp_home.mkdir()
    (ssp_home / "local-executor.sqlite").write_bytes(os.urandom(64))

    result = runner.invoke(app, _command(_VALID, "--yes"))
    record = _tenant(ssp_home, "demo")

    assert result.exit_code == 1
    assert result.stderr != ""
    assert "Traceback" not in result.stderr
    assert result.stderr.splitlines()[-1] == (
        "tenant is left in status creating; see ssp status demo"
    )
    assert record.status is TenantStatus.CREATING


def test_component_name_controls_are_escaped_in_the_plan(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    name = "up\nloads\x1b"
    manifest = tmp_path / "named.yaml"
    manifest.write_text(
        "- type: s3-bucket\n"
        '  name: "up\\nloads\\x1b"\n'
        "- type: sqs-queue\n"
        "  name: jobs\n",
        encoding="utf-8",
    )
    loaded = load_manifest_from_file(manifest)
    assert loaded.root[0].name == name

    result = runner.invoke(app, _command(manifest, "--plan-only"))
    lines = result.stdout.splitlines()
    escaped = f"{_escape_stored(name)} s3-bucket"

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert "\x1b" not in result.stdout
    assert escaped == "up\\nloads\\x1b s3-bucket"
    assert escaped in lines
    assert lines[lines.index(escaped) + 1] == "  S3 storage not priced"
    assert "jobs sqs-queue" in lines
    assert not ssp_home.exists()


def test_concurrent_creates_of_one_id_keep_one_tenant(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    # Proves one winner and no corruption, not that a race necessarily happened.
    # The registry-level two-connection race test is the real lock on atomicity.
    first = _start_create(ssp_home, "demo", _VALID, tmp_path)
    second = _start_create(ssp_home, "demo", _VALID, tmp_path)
    out_first, err_first = first.communicate(timeout=60)
    out_second, err_second = second.communicate(timeout=60)
    succeeded = out_first if first.returncode == 0 else out_second
    failed = err_second if first.returncode == 0 else err_first
    loser = second.returncode if first.returncode == 0 else first.returncode
    executable = shutil.which("ssp")
    assert executable is not None
    env = os.environ.copy()
    env["SSP_HOME"] = str(ssp_home)
    third = subprocess.run(
        [
            executable,
            "create",
            "demo",
            str(_VALID),
            "--type",
            "dev",
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

    assert (first.returncode == 0) ^ (second.returncode == 0)
    assert loser in {1, 2}
    assert failed.strip() != ""
    assert len(failed.splitlines()) <= 3
    assert "Traceback" not in err_first
    assert "Traceback" not in err_second
    assert "created: demo status active" in succeeded.splitlines()
    assert _tenant(ssp_home, "demo").status is TenantStatus.ACTIVE
    _integrity_ok(ssp_home / "tenants.sqlite")
    _integrity_ok(ssp_home / "local-executor.sqlite")
    assert third.returncode == 1
    assert third.stderr == "tenant already exists: demo\n"
    assert "Traceback" not in third.stderr


def test_concurrent_creates_of_different_ids_both_succeed(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    first = _start_create(ssp_home, "demo", _VALID, tmp_path)
    second = _start_create(ssp_home, "other", _VALID, tmp_path)
    first.communicate(timeout=60)
    second.communicate(timeout=60)

    assert first.returncode == 0
    assert second.returncode == 0
    assert _tenant(ssp_home, "demo").status is TenantStatus.ACTIVE
    assert _tenant(ssp_home, "other").status is TenantStatus.ACTIVE
    _integrity_ok(ssp_home / "tenants.sqlite")
    _integrity_ok(ssp_home / "local-executor.sqlite")


def test_yes_flag_does_not_print_the_prompt(ssp_home: Path) -> None:
    result = runner.invoke(app, _command(_VALID, "--yes"))

    assert result.exit_code == 0
    assert _PROMPT not in result.stdout.splitlines()
    assert _PROMPT not in result.stderr


def test_end_of_file_counts_as_declined(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, True)
    declined = runner.invoke(app, _command(_VALID), input="n\n")
    ended = runner.invoke(app, _command(_VALID), input="")

    assert ended.exit_code == declined.exit_code == 1
    assert ended.stderr == declined.stderr == ""
    assert ended.stdout.splitlines()[-1] == _PROMPT
    assert declined.stdout.splitlines()[-1] == _PROMPT
    assert "created:" not in ended.stdout
    assert "created:" not in declined.stdout
    assert not ssp_home.exists()

import getpass
import os
import shutil
import sqlite3
import subprocess
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner, Result

from self_service_platform.app import app
from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.cli.main import _escape_stored, create_app
from self_service_platform.core.executor import ProvisionRequest, ResourceRef
from self_service_platform.core.manifest import (
    Manifest,
    ManifestEntry,
    load_manifest_from_file,
)
from self_service_platform.core.registry import Clock
from self_service_platform.core.service import (
    DestroyPlan,
    PlatformService,
    TtlPolicy,
    UpdateConfirmations,
    UpdateFailedError,
    UpdatePlan,
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
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

runner = CliRunner()
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_VALID = _EXAMPLES / "valid-manifest.yaml"
_USER_UNKNOWN = "cannot determine the operating system user\n"
_NO_TENANT = "no such tenant: demo\n"
_APPLY = "Apply this update? [y/N]"
_DELETE_JOBS = "Delete the data of these components: jobs? [y/N]"
_RECREATE_UPLOADS = "Recreate uploads (its data is lost)? [y/N]"
_DESTROY_PROMPT = "Type the tenant id to confirm destroy:"
_PASS_YES = "confirmation required; pass --yes\n"
_UNSAFE_ID = "cannot confirm this tenant id interactively; pass --yes\n"
_DATA_LOSS = "(data in this component will be deleted)"
_STATEMENT = "all resources and data of the tenant will be deleted"
_CHANGED = (
    "tenant changed while the command was running; no change was made by this command\n"
)
_WITH_REPORTS = """\
- type: s3-bucket
  name: uploads
- type: sqs-queue
  name: jobs
- type: sqs-queue
  name: reports
"""
_UPLOADS_ONLY = """\
- type: s3-bucket
  name: uploads
"""
_BUCKET = """\
- type: versioned-bucket
  name: uploads
- type: sqs-queue
  name: jobs
"""
_BUCKET_ENCRYPTION = """\
- type: versioned-bucket
  name: uploads
  options:
    encryption: true
- type: sqs-queue
  name: jobs
"""
_BUCKET_VERSIONING = """\
- type: versioned-bucket
  name: uploads
  options:
    versioning: true
- type: sqs-queue
  name: jobs
"""
_BUCKET_REPLACE_AND_REMOVE = """\
- type: versioned-bucket
  name: uploads
  options:
    versioning: true
"""
_T = TypeVar("_T")


class _BucketOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False
    encryption: bool = False


class _QueueOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


@pytest.fixture(autouse=True)
def ssp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("SSP_HOME", str(home))
    return home


def _text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _estimate(*components: tuple[str, str, str]) -> tuple[str, ...]:
    lines: list[str] = []
    for name, component_type, description in components:
        lines.append(f"{_escape_stored(name)} {_escape_stored(component_type)}")
        lines.append(f"  {_escape_stored(description)} not priced")
        lines.append("  total: not priced")
    lines.append("manifest total: not priced")
    lines.append("POC heuristic")
    return tuple(lines)


_BOTH = _estimate(
    ("uploads", "s3-bucket", "S3 storage"),
    ("jobs", "sqs-queue", "SQS requests"),
)
_THREE = _estimate(
    ("uploads", "s3-bucket", "S3 storage"),
    ("jobs", "sqs-queue", "SQS requests"),
    ("reports", "sqs-queue", "SQS requests"),
)
_UPLOADS = _estimate(("uploads", "s3-bucket", "S3 storage"))
_BUCKET_BOTH = _estimate(
    ("uploads", "versioned-bucket", "storage"),
    ("jobs", "sqs-queue", "requests"),
)
_BUCKET_ONE = _estimate(("uploads", "versioned-bucket", "storage"))


def _update_text(
    diff: tuple[str, ...],
    estimate: tuple[str, ...],
    removals: str,
    replacements: str,
) -> str:
    return _text(
        "tenant_id: demo",
        "status: active",
        *diff,
        *estimate,
        f"removals: {removals}",
        f"replacements: {replacements}",
    )


def _destroy_text(
    *names: str,
    tenant_id: str = "demo",
    tenant_type: str = "dev",
    status: str = "active",
) -> str:
    return _text(
        f"tenant_id: {_escape_stored(tenant_id)}",
        f"tenant_type: {_escape_stored(tenant_type)}",
        f"status: {_escape_stored(status)}",
        *(_escape_stored(name) for name in names),
        _STATEMENT,
    )


def _terminal(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(
        "self_service_platform.cli.main._stdin_is_terminal",
        lambda: value,
    )


def _patch_getuser(monkeypatch: pytest.MonkeyPatch, getuser: Callable[[], str]) -> None:
    monkeypatch.setattr(getpass, "getuser", getuser)
    module = __import__("self_service_platform.cli.main", fromlist=["getuser"])
    if hasattr(module, "getuser"):
        monkeypatch.setattr(module, "getuser", getuser)


def _manifest(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _record(home: Path, tenant_id: str = "demo") -> TenantRecord:
    registry = SqliteRegistry.open_read_only(home / "tenants.sqlite")
    try:
        return registry.get(tenant_id)
    finally:
        registry.close()


def _resource_ids(home: Path, tenant_id: str = "demo") -> tuple[str, ...]:
    executor = LocalExecutor(home / "local-executor.sqlite")
    resources = executor.list_tagged_resources(tenant_id)
    return tuple(resource.identifier for resource in resources)


def _assert_same(
    home: Path,
    before: TenantRecord,
    resources: tuple[str, ...],
) -> None:
    assert _record(home, before.tenant_id) == before
    assert _resource_ids(home, before.tenant_id) == resources


def _create_real(
    home: Path,
    manifest: Path = _VALID,
    tenant_id: str = "demo",
    owner: str = "alice",
) -> TenantRecord:
    result = runner.invoke(
        app,
        [
            "create",
            tenant_id,
            str(manifest),
            "--type",
            "dev",
            "--owner",
            owner,
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stderr
    return _record(home, tenant_id)


def _update(manifest: Path, *options: str, tenant_id: str = "demo") -> list[str]:
    return ["update", tenant_id, str(manifest), *options]


def _destroy(tenant_id: str = "demo", *options: str) -> list[str]:
    return ["destroy", tenant_id, *options]


def _after_block(
    header: str,
    before: TenantRecord,
    components: tuple[str, ...],
    status: str,
) -> str:
    assert before.expires_at is not None
    return _text(
        header,
        f"status: {status}",
        f"tenant_type: {before.tenant_type.value}",
        f"owner: {_escape_stored(before.owner)}",
        f"initiator: cli {_escape_stored(getpass.getuser())}",
        f"catalog_version: {before.catalog_version}",
        f"expires_at: {before.expires_at.isoformat()}",
        "last_error: -",
        *components,
    )


def _arm_init(
    monkeypatch: pytest.MonkeyPatch,
    hook: Callable[[LocalExecutor], None],
) -> Callable[..., None]:
    original = LocalExecutor.__init__

    def armed(self: LocalExecutor, path: Path) -> None:
        original(self, path)
        hook(self)

    monkeypatch.setattr(LocalExecutor, "__init__", armed)
    return original


def _forbid_executor(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def forbid(name: str) -> Callable[..., None]:
        def method(self: LocalExecutor, *args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(name)

        return method

    monkeypatch.setattr(LocalExecutor, "provision", forbid("provision"))
    monkeypatch.setattr(LocalExecutor, "destroy", forbid("destroy"))
    monkeypatch.setattr(
        LocalExecutor,
        "list_tagged_resources",
        forbid("list_tagged_resources"),
    )
    monkeypatch.setattr(
        LocalExecutor, "tenant_role_exists", forbid("tenant_role_exists")
    )


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


class _OpeningRegistry:
    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock

    def create_tenant(self, record: TenantRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._use(lambda registry: registry.create_tenant(record))

    def get(self, tenant_id: str) -> TenantRecord:
        return self._use(lambda registry: registry.get(tenant_id))

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._use(lambda registry: registry.list_tenants())

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        return self._use(
            lambda registry: registry.transition(tenant_id, expected, new, fields)
        )

    def close(self) -> None:
        return

    def _use(self, work: Callable[[SqliteRegistry], _T]) -> _T:
        registry = SqliteRegistry(self._path, self._clock)
        try:
            return work(registry)
        finally:
            registry.close()


class _CountingExecutor:
    def __init__(self, inner: LocalExecutor) -> None:
        self._inner = inner
        self.provision_calls = 0
        self.destroy_calls = 0
        self.calls: list[str] = []
        self._raise_provision: BaseException | None = None
        self._raise_destroy: BaseException | None = None

    def provision(self, request: ProvisionRequest) -> None:
        self.calls.append("provision")
        if self._raise_provision is not None:
            error = self._raise_provision
            self._raise_provision = None
            raise error
        self.provision_calls += 1
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self.calls.append("destroy")
        if self._raise_destroy is not None:
            error = self._raise_destroy
            self._raise_destroy = None
            raise error
        self.destroy_calls += 1
        self._inner.destroy(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        self.calls.append("list_tagged_resources")
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        self.calls.append("tenant_role_exists")
        return self._inner.tenant_role_exists(tenant_id)

    def fail_next_provision(self) -> None:
        self._inner.fail_next_provision()

    def raise_on_next_provision(self, error: BaseException) -> None:
        self._raise_provision = error

    def raise_on_next_destroy(self, error: BaseException) -> None:
        self._raise_destroy = error


class _RejectingRegistry:
    def __init__(self, inner: _OpeningRegistry, new_status: TenantStatus) -> None:
        self._inner = inner
        self._new_status = new_status

    def create_tenant(self, record: TenantRecord) -> None:
        self._inner.create_tenant(record)

    def get(self, tenant_id: str) -> TenantRecord:
        return self._inner.get(tenant_id)

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._inner.list_tenants()

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        if new is self._new_status:
            raise TransitionRejectedError("lost the conditional write")
        return self._inner.transition(tenant_id, expected, new, fields)

    def close(self) -> None:
        self._inner.close()


class _Stack:
    def __init__(
        self,
        home: Path,
        catalog: Catalog,
        reject: TenantStatus | None = None,
    ) -> None:
        def clock() -> datetime:
            return datetime.now(UTC)

        self.registry = _OpeningRegistry(home / "tenants.sqlite", clock)
        self.executor = _CountingExecutor(LocalExecutor(home / "local-executor.sqlite"))
        registry = self.registry
        if reject is not None:
            registry = _RejectingRegistry(self.registry, reject)
        self.service = PlatformService(
            catalog,
            registry,
            self.executor,
            clock,
            TtlPolicy(dev=timedelta(days=1), ephemeral=timedelta(hours=3)),
        )
        self.cli = create_app(
            catalog,
            open_registry=lambda: self.registry,
            open_service=lambda: self.service,
        )


def _removal(service: PlatformService, manifest: Manifest) -> None:
    plan = service.plan_update("demo", manifest)
    service.apply_update(
        plan,
        UpdateConfirmations(update=True, removals=True, replacements=frozenset()),
        Initiator(kind=InitiatorKind.CLI, owner="other"),
    )


def _failed_update(
    service: PlatformService,
    executor: _CountingExecutor,
    manifest: Manifest,
) -> None:
    executor.fail_next_provision()
    plan = service.plan_update("demo", manifest)
    with pytest.raises(UpdateFailedError):
        service.apply_update(
            plan,
            UpdateConfirmations(update=True, removals=False, replacements=frozenset()),
            Initiator(kind=InitiatorKind.CLI, owner="other"),
        )


class _Snapshot:
    def __init__(self) -> None:
        self.record: TenantRecord | None = None
        self.provision_calls = 0
        self.destroy_calls = 0


def _other_update_after_plan(
    stack: _Stack,
    change: Callable[[], None],
    command: str,
) -> _Snapshot:
    snapshot = _Snapshot()

    def remember(tenant_id: str) -> None:
        if snapshot.record is not None:
            return
        change()
        snapshot.record = stack.registry.get(tenant_id)
        snapshot.provision_calls = stack.executor.provision_calls
        snapshot.destroy_calls = stack.executor.destroy_calls

    if command == "update":
        original = stack.service.plan_update

        def plan_update(tenant_id: str, manifest: Manifest) -> UpdatePlan:
            plan = original(tenant_id, manifest)
            remember(tenant_id)
            return plan

        stack.service.plan_update = plan_update
    else:
        original_destroy = stack.service.plan_destroy

        def plan_destroy(tenant_id: str) -> DestroyPlan:
            plan = original_destroy(tenant_id)
            remember(tenant_id)
            return plan

        stack.service.plan_destroy = plan_destroy
    return snapshot


def _seed_active(home: Path, tenant_id: str) -> TenantRecord:
    # ssp create rejects a control character in the tenant id.
    home.mkdir()
    moment = datetime.now(UTC)
    manifest = (
        ManifestEntry(type="s3-bucket", name="uploads"),
        ManifestEntry(type="sqs-queue", name="jobs"),
    )
    initiator = Initiator(kind=InitiatorKind.CLI, owner="seed")
    version = build_catalog().catalog_version
    expires = moment + timedelta(days=1)
    registry = SqliteRegistry(home / "tenants.sqlite", lambda: moment)
    try:
        registry.create_tenant(
            TenantRecord(
                tenant_id=tenant_id,
                tenant_type=TenantType.DEV,
                owner="alice",
                status=TenantStatus.CREATING,
                manifest=manifest,
                catalog_version=version,
                initiator=initiator,
                expires_at=expires,
                last_error=None,
                updated_at=moment,
            )
        )
        registry.transition(
            tenant_id,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            TransitionFields(
                manifest=manifest,
                catalog_version=version,
                initiator=initiator,
                expires_at=expires,
                last_error=None,
            ),
        )
    finally:
        registry.close()
    return _record(home, tenant_id)


def _create_on(stack: _Stack, manifest: Path, tenant_id: str = "demo") -> TenantRecord:
    result = runner.invoke(
        stack.cli,
        [
            "create",
            tenant_id,
            str(manifest),
            "--type",
            "dev",
            "--owner",
            "alice",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stderr
    return stack.registry.get(tenant_id)


def _awkward(directory: Path) -> Path:
    name = "up\\nloads\\x1b"
    text = f'- type: s3-bucket\n  name: "{name}"\n- type: sqs-queue\n  name: jobs\n'
    return _manifest(directory, "awkward.yaml", text)


@pytest.mark.parametrize("error", [OSError, KeyError, ImportError])
def test_operating_system_user_errors_write_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    # The guard lives in the shared operating-system user helper.
    def broken() -> str:
        raise error("no user")

    _patch_getuser(monkeypatch, broken)

    result = runner.invoke(
        app,
        ["create", "demo", str(_VALID), "--type", "dev", "--owner", "alice", "--yes"],
    )

    assert result.exit_code == 2
    assert result.stderr == _USER_UNKNOWN
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_runtime_error_from_getuser_is_not_swallowed(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> str:
        raise RuntimeError("no user")

    _patch_getuser(monkeypatch, broken)

    result = runner.invoke(
        app,
        ["create", "demo", str(_VALID), "--type", "dev", "--owner", "alice", "--yes"],
    )

    assert isinstance(result.exception, RuntimeError)
    assert result.exit_code != 2
    assert _USER_UNKNOWN.strip() not in result.stderr
    assert not ssp_home.exists()


def test_update_plan_prints_an_added_component(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(added, "--plan-only"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _update_text(
        ("added: reports sqs-queue", "summary: 1 added, 0 removed, 0 changed"),
        _THREE,
        "-",
        "-",
    )


def test_update_plan_prints_a_removed_component(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(removed, "--plan-only"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _update_text(
        (
            f"removed: jobs sqs-queue {_DATA_LOSS}",
            "summary: 0 added, 1 removed, 0 changed",
        ),
        _UPLOADS,
        "jobs",
        "-",
    )


def test_update_plan_prints_a_changed_option(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_ENCRYPTION)
    _terminal(monkeypatch, True)

    result = runner.invoke(stack.cli, _update(changed, "--plan-only"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _update_text(
        (
            "changed: uploads versioned-bucket options: encryption",
            "summary: 0 added, 0 removed, 1 changed",
        ),
        _BUCKET_BOTH,
        "-",
        "-",
    )


def test_update_plan_prints_a_replacement(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, True)

    result = runner.invoke(stack.cli, _update(changed, "--plan-only"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _update_text(
        (
            "changed: uploads versioned-bucket options: versioning [REPLACEMENT]",
            "summary: 0 added, 0 removed, 1 changed",
        ),
        _BUCKET_BOTH,
        "-",
        "uploads",
    )


def test_update_with_no_changes_writes_nothing_and_asks_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(_VALID), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _update_text(("no changes",), _BOTH, "-", "-")
    assert _APPLY not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_plan_only_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, True)

    result = runner.invoke(
        app,
        _update(removed, "--plan-only", "--yes", "--confirm-removals"),
        input="n\n",
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _APPLY not in result.stdout
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_plan_only_on_a_missing_state_creates_nothing(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    missing = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)

    result = runner.invoke(app, _update(missing, "--plan-only", "--yes"))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _NO_TENANT
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_update_yes_alone_is_refused_when_there_are_removals(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, False)

    result = runner.invoke(app, _update(removed, "--yes"), input="y\n")

    assert result.exit_code == 1
    assert result.stderr == "confirmation required; pass --confirm-removals\n"
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_yes_alone_is_refused_when_there_are_replacements(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    before = _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    resources = _resource_ids(ssp_home)
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, False)

    result = runner.invoke(stack.cli, _update(changed, "--yes"), input="y\n")

    assert result.exit_code == 1
    assert (
        result.stderr == "confirmation required; pass --confirm-replacement uploads\n"
    )
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_confirm_removals_alone_is_refused(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, False)

    result = runner.invoke(app, _update(removed, "--confirm-removals"), input="y\n")

    assert result.exit_code == 1
    assert result.stderr == _PASS_YES
    assert "Traceback" not in result.stderr
    _assert_same(ssp_home, before, resources)


def test_update_confirm_replacement_alone_is_refused(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    before = _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    resources = _resource_ids(ssp_home)
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, False)

    result = runner.invoke(
        stack.cli,
        _update(changed, "--confirm-replacement", "uploads"),
        input="y\n",
    )

    assert result.exit_code == 1
    assert result.stderr == _PASS_YES
    assert "Traceback" not in result.stderr
    _assert_same(ssp_home, before, resources)


def test_update_with_every_confirmation_flag_succeeds(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_REPLACE_AND_REMOVE)
    _terminal(monkeypatch, True)

    result = runner.invoke(
        stack.cli,
        _update(
            changed,
            "--yes",
            "--confirm-removals",
            "--confirm-replacement",
            "uploads",
        ),
        input="n\n",
    )
    record = stack.registry.get("demo")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _APPLY not in result.stdout
    assert _DELETE_JOBS not in result.stdout
    assert _RECREATE_UPLOADS not in result.stdout
    assert "updated: demo status active" in result.stdout.splitlines()
    assert record.status is TenantStatus.ACTIVE
    assert tuple(entry.name for entry in record.manifest) == ("uploads",)
    assert record.manifest[0].options["versioning"] is True


def test_update_confirm_replacement_given_twice_succeeds_once(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, True)

    result = runner.invoke(
        stack.cli,
        _update(
            changed,
            "--yes",
            "--confirm-replacement",
            "uploads",
            "--confirm-replacement",
            "uploads",
        ),
        input="n\n",
    )
    record = stack.registry.get("demo")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _RECREATE_UPLOADS not in result.stdout
    assert record.status is TenantStatus.ACTIVE
    assert record.manifest[0].options["versioning"] is True


def test_update_unknown_replacement_name_does_not_confirm_the_real_one(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    before = _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    resources = _resource_ids(ssp_home)
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, False)

    result = runner.invoke(
        stack.cli,
        _update(changed, "--yes", "--confirm-replacement", "nope"),
        input="y\n",
    )

    assert result.exit_code == 1
    assert (
        result.stderr == "confirmation required; pass --confirm-replacement uploads\n"
    )
    assert "nope" not in result.stderr
    assert "Traceback" not in result.stderr
    _assert_same(ssp_home, before, resources)


def test_update_interactive_yes_applies(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(added), input="y\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        _update_text(
            ("added: reports sqs-queue", "summary: 1 added, 0 removed, 0 changed"),
            _THREE,
            "-",
            "-",
        )
        + f"{_APPLY}\n"
        + _after_block(
            "updated: demo status active",
            before,
            ("uploads s3-bucket", "jobs sqs-queue", "reports sqs-queue"),
            "active",
        )
    )


@pytest.mark.parametrize("stdin", ["n\n", "\n", ""], ids=["no", "empty", "eof"])
def test_update_declining_the_apply_prompt_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(added), input=stdin)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert _APPLY in result.stdout
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_interactive_removal_yes_applies(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(removed), input="y\ny\n")
    record = _record(ssp_home)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.index(_APPLY) < result.stdout.index(_DELETE_JOBS)
    assert "updated: demo status active" in result.stdout.splitlines()
    assert tuple(entry.name for entry in record.manifest) == ("uploads",)


@pytest.mark.parametrize(
    "stdin",
    ["y\nn\n", "y\n\n", "y\n"],
    ids=["no", "empty", "eof"],
)
def test_update_declining_the_removal_prompt_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    removed = _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _update(removed), input=stdin)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.index(_APPLY) < result.stdout.index(_DELETE_JOBS)
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_interactive_replacement_yes_applies(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, True)

    result = runner.invoke(stack.cli, _update(changed), input="y\ny\n")
    record = stack.registry.get("demo")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.index(_APPLY) < result.stdout.index(_RECREATE_UPLOADS)
    assert record.manifest[0].options["versioning"] is True


@pytest.mark.parametrize(
    "stdin",
    ["y\nn\n", "y\n\n", "y\n"],
    ids=["no", "empty", "eof"],
)
def test_update_declining_the_replacement_prompt_writes_nothing(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    before = _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    resources = _resource_ids(ssp_home)
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_VERSIONING)
    _terminal(monkeypatch, True)

    result = runner.invoke(stack.cli, _update(changed), input=stdin)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.index(_APPLY) < result.stdout.index(_RECREATE_UPLOADS)
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_without_a_terminal_refuses_missing_flags_without_reading_stdin(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, _replacement_catalog())
    before = _create_on(stack, _manifest(tmp_path, "base.yaml", _BUCKET))
    resources = _resource_ids(ssp_home)
    changed = _manifest(tmp_path, "changed.yaml", _BUCKET_REPLACE_AND_REMOVE)
    _terminal(monkeypatch, False)

    result = runner.invoke(stack.cli, _update(changed), input="y\ny\ny\n")

    assert result.exit_code == 1
    assert result.stderr == (
        "confirmation required; pass --yes --confirm-removals "
        "--confirm-replacement uploads\n"
    )
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_update_of_an_unknown_tenant_creates_nothing(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    missing = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)

    result = runner.invoke(app, _update(missing, "--yes"))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _NO_TENANT
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_update_stale_plan_exits_without_writes(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    removal = load_manifest_from_file(
        _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    )
    original = stack.service.plan_update

    def change() -> None:
        plan = original("demo", removal)
        stack.service.apply_update(
            plan,
            UpdateConfirmations(update=True, removals=True, replacements=frozenset()),
            Initiator(kind=InitiatorKind.CLI, owner="other"),
        )

    snapshot = _other_update_after_plan(stack, change, "update")

    result = runner.invoke(
        stack.cli,
        _update(_manifest(tmp_path, "added.yaml", _WITH_REPORTS), "--yes"),
    )

    assert result.exit_code == 1
    assert result.stderr == "stored manifest does not match the plan\n"
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    assert snapshot.record is not None
    assert stack.registry.get("demo") == snapshot.record
    assert tuple(entry.name for entry in snapshot.record.manifest) == ("uploads",)
    assert stack.executor.provision_calls == snapshot.provision_calls


def test_update_changed_during_the_prompt_exits_without_writes(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    removal = load_manifest_from_file(
        _manifest(tmp_path, "removed.yaml", _UPLOADS_ONLY)
    )
    seen: dict[str, TenantRecord] = {}
    counts: dict[str, int] = {}

    def answer() -> str:
        # The prompt is waiting inside input(); the other update runs first.
        if "record" not in seen:
            _removal(stack.service, removal)
            seen["record"] = stack.registry.get("demo")
            counts["provision"] = stack.executor.provision_calls
        return "y"

    _terminal(monkeypatch, True)
    monkeypatch.setattr("builtins.input", answer)

    result = runner.invoke(
        stack.cli,
        _update(_manifest(tmp_path, "added.yaml", _WITH_REPORTS)),
    )

    assert result.exit_code == 1
    assert result.stderr == "stored manifest does not match the plan\n"
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    assert stack.registry.get("demo") == seen["record"]
    assert stack.executor.provision_calls == counts["provision"]


def test_update_failure_leaves_update_failed_and_a_later_update_succeeds(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
    original = _arm_init(monkeypatch, lambda executor: executor.fail_next_provision())

    failed = runner.invoke(app, _update(added, "--yes"))
    status = runner.invoke(app, ["status", "demo"])
    monkeypatch.setattr(LocalExecutor, "__init__", original)
    corrected = runner.invoke(app, _update(added, "--yes"))
    record = _record(ssp_home)

    assert failed.exit_code == 1
    assert failed.stderr == (
        "injected provision failure\n"
        "tenant is in status update_failed; see ssp status demo\n"
    )
    assert "Traceback" not in failed.stderr
    assert "updated:" not in failed.stdout
    assert status.exit_code == 0
    assert status.stdout.splitlines()[0] == "status: update_failed"
    assert corrected.exit_code == 0
    assert corrected.stderr == ""
    assert "updated: demo status active" in corrected.stdout.splitlines()
    assert record.status is TenantStatus.ACTIVE
    assert tuple(entry.name for entry in record.manifest) == (
        "uploads",
        "jobs",
        "reports",
    )


def test_update_of_a_destroyed_tenant_names_the_status(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    destroyed = runner.invoke(app, _destroy("demo", "--yes"))
    calls: list[str] = []
    _forbid_executor(monkeypatch, calls)
    before = _record(ssp_home)

    result = runner.invoke(app, _update(_VALID, "--yes"))

    assert destroyed.exit_code == 0
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "cannot update status destroyed\n"
    assert "Traceback" not in result.stderr
    assert calls == []
    assert _record(ssp_home) == before


def test_update_output_escapes_stored_strings(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    name = "up\nloads\x1b"
    owner = "caf\u00e9"
    created = _awkward(tmp_path)
    extra = "- type: sqs-queue\n  name: reports\n"
    proposed = _manifest(
        tmp_path, "proposed.yaml", created.read_text(encoding="utf-8") + extra
    )
    before = _create_real(ssp_home, created, owner=owner)
    loaded = load_manifest_from_file(created)
    assert loaded.root[0].name == name

    result = runner.invoke(app, _update(proposed, "--yes"))
    lines = result.stdout.splitlines()
    escaped = _escape_stored(name)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert "\x1b" not in result.stdout
    assert escaped == "up\\nloads\\x1b"
    assert f"{escaped} s3-bucket" in lines
    assert f"owner: {_escape_stored(owner)}" in lines
    assert before.owner == owner


def test_destroy_plan_lists_the_components_and_the_statement(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy("demo", "--plan-only"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == _destroy_text("uploads", "jobs")


def test_destroy_plan_only_writes_nothing(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy("demo", "--plan-only", "--yes"), input="n\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _DESTROY_PROMPT not in result.stdout
    assert "destroyed:" not in result.stdout
    assert _STATEMENT in result.stdout
    _assert_same(ssp_home, before, resources)


def test_destroy_plan_only_on_a_missing_state_creates_nothing(ssp_home: Path) -> None:
    result = runner.invoke(app, _destroy("demo", "--plan-only", "--yes"))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _NO_TENANT
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_destroy_yes_destroys_the_tenant(ssp_home: Path) -> None:
    before = _create_real(ssp_home)

    result = runner.invoke(app, _destroy("demo", "--yes"))
    record = _record(ssp_home)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert _DESTROY_PROMPT not in result.stdout
    assert result.stdout == (
        _destroy_text("uploads", "jobs")
        + _after_block(
            "destroyed: demo status destroyed",
            before,
            ("uploads s3-bucket", "jobs sqs-queue"),
            "destroyed",
        )
    )
    assert record.status is TenantStatus.DESTROYED
    assert _resource_ids(ssp_home) == ()


def test_destroy_interactive_exact_id_destroys(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy("demo"), input="demo\n")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        _destroy_text("uploads", "jobs")
        + f"{_DESTROY_PROMPT}\n"
        + _after_block(
            "destroyed: demo status destroyed",
            before,
            ("uploads s3-bucket", "jobs sqs-queue"),
            "destroyed",
        )
    )


@pytest.mark.parametrize(
    "stdin",
    ["Demo\n", "y\n", "\n", ""],
    ids=["wrong", "y", "empty", "eof"],
)
def test_destroy_interactive_rejects_anything_but_the_tenant_id(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy("demo"), input=stdin)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert _DESTROY_PROMPT in result.stdout
    assert "destroyed:" not in result.stdout
    _assert_same(ssp_home, before, resources)


@pytest.mark.parametrize("stdin", [" demo\n", "demo \n"], ids=["leading", "trailing"])
def test_destroy_rejects_surrounding_whitespace(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy("demo"), input=stdin)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert "destroyed:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_destroy_without_a_terminal_requires_yes_without_reading_stdin(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _create_real(ssp_home)
    resources = _resource_ids(ssp_home)
    _terminal(monkeypatch, False)

    result = runner.invoke(app, _destroy("demo"), input="demo\n")

    assert result.exit_code == 1
    assert result.stdout != ""
    assert result.stderr == _PASS_YES
    assert "Traceback" not in result.stderr
    assert "destroyed:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_destroy_of_an_unknown_tenant_creates_nothing(ssp_home: Path) -> None:
    result = runner.invoke(app, _destroy("demo", "--yes"))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _NO_TENANT
    assert "Traceback" not in result.stderr
    assert not ssp_home.exists()


def test_destroy_stale_plan_exits_without_writes(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    added = load_manifest_from_file(_manifest(tmp_path, "added.yaml", _WITH_REPORTS))
    snapshot = _other_update_after_plan(
        stack,
        lambda: _failed_update(stack.service, stack.executor, added),
        "destroy",
    )

    result = runner.invoke(stack.cli, _destroy("demo", "--yes"))

    assert result.exit_code == 1
    assert result.stderr == "stored status does not match the plan\n"
    assert "Traceback" not in result.stderr
    assert "destroyed:" not in result.stdout
    assert snapshot.record is not None
    assert snapshot.record.status is TenantStatus.UPDATE_FAILED
    assert stack.registry.get("demo") == snapshot.record
    assert stack.executor.destroy_calls == snapshot.destroy_calls


def test_destroy_changed_during_the_prompt_exits_without_writes(
    tmp_path: Path,
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    added = load_manifest_from_file(_manifest(tmp_path, "added.yaml", _WITH_REPORTS))
    seen: dict[str, TenantRecord] = {}
    counts: dict[str, int] = {}

    def answer() -> str:
        # The prompt is waiting inside input(); the other update runs first.
        if "record" not in seen:
            _failed_update(stack.service, stack.executor, added)
            seen["record"] = stack.registry.get("demo")
            counts["destroy"] = stack.executor.destroy_calls
        return "demo"

    _terminal(monkeypatch, True)
    monkeypatch.setattr("builtins.input", answer)

    result = runner.invoke(stack.cli, _destroy("demo"))

    assert result.exit_code == 1
    assert result.stderr == "stored status does not match the plan\n"
    assert "Traceback" not in result.stderr
    assert "destroyed:" not in result.stdout
    assert seen["record"].status is TenantStatus.UPDATE_FAILED
    assert stack.registry.get("demo") == seen["record"]
    assert stack.executor.destroy_calls == counts["destroy"]


def test_destroy_of_a_creating_tenant_names_the_status(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _arm_init(monkeypatch, lambda executor: executor.fail_next_provision())
    failed = runner.invoke(
        app,
        ["create", "demo", str(_VALID), "--type", "dev", "--owner", "alice", "--yes"],
    )
    monkeypatch.setattr(LocalExecutor, "__init__", original)
    calls: list[str] = []
    _forbid_executor(monkeypatch, calls)
    before = _record(ssp_home)

    result = runner.invoke(app, _destroy("demo", "--yes"))

    assert failed.exit_code == 1
    assert before.status is TenantStatus.CREATING
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "cannot destroy status creating\n"
    assert "Traceback" not in result.stderr
    assert calls == []
    assert _record(ssp_home) == before


def test_destroy_of_a_destroyed_tenant_names_the_status(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    destroyed = runner.invoke(app, _destroy("demo", "--yes"))
    calls: list[str] = []
    _forbid_executor(monkeypatch, calls)
    before = _record(ssp_home)

    result = runner.invoke(app, _destroy("demo", "--yes"))

    assert destroyed.exit_code == 0
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "cannot destroy status destroyed\n"
    assert "Traceback" not in result.stderr
    assert calls == []
    assert _record(ssp_home) == before


def test_destroy_leftover_resource_ends_destroy_failed(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_real(ssp_home)
    _arm_init(
        monkeypatch, lambda executor: executor.leave_one_resource_on_next_destroy()
    )

    result = runner.invoke(app, _destroy("demo", "--yes"))
    status = runner.invoke(app, ["status", "demo"])

    assert result.exit_code == 1
    assert result.stderr == (
        "leftover resources remain: demo/uploads\n"
        "tenant is in status destroy_failed; see ssp status demo\n"
    )
    assert "Traceback" not in result.stderr
    assert "destroyed:" not in result.stdout
    assert status.exit_code == 0
    assert status.stdout.splitlines()[0] == "status: destroy_failed"


def test_destroy_refuses_interactive_confirmation_for_a_control_character_id(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = "de\x1bmo"
    before = _seed_active(ssp_home, tenant_id)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy(tenant_id), input=f"{tenant_id}\n")

    assert result.exit_code == 1
    assert result.stderr == _UNSAFE_ID
    assert "Traceback" not in result.stderr
    assert result.stdout.isascii()
    assert "\x1b" not in result.stdout
    assert _DESTROY_PROMPT not in result.stdout
    assert _STATEMENT in result.stdout
    assert "destroyed:" not in result.stdout
    assert _record(ssp_home, tenant_id) == before
    assert not (ssp_home / "local-executor.sqlite").exists()


def test_destroy_refuses_interactive_confirmation_for_a_non_ascii_id(
    ssp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = "d\u00e9mo"
    before = _create_real(ssp_home, tenant_id=tenant_id)
    resources = _resource_ids(ssp_home, tenant_id)
    _terminal(monkeypatch, True)

    result = runner.invoke(app, _destroy(tenant_id), input=f"{tenant_id}\n")

    assert result.exit_code == 1
    assert result.stderr == _UNSAFE_ID
    assert "Traceback" not in result.stderr
    assert result.stdout.isascii()
    assert _DESTROY_PROMPT not in result.stdout
    assert _STATEMENT in result.stdout
    assert "destroyed:" not in result.stdout
    _assert_same(ssp_home, before, resources)


def test_destroy_yes_works_for_an_unsafe_tenant_id(ssp_home: Path) -> None:
    tenant_id = "de\x1bmo"
    _seed_active(ssp_home, tenant_id)

    result = runner.invoke(app, _destroy(tenant_id, "--yes"))
    status = runner.invoke(app, ["status", tenant_id])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert "\x1b" not in result.stdout
    assert (
        f"destroyed: {_escape_stored(tenant_id)} status destroyed"
        in result.stdout.splitlines()
    )
    assert _DESTROY_PROMPT not in result.stdout
    assert status.exit_code == 0
    assert status.stdout.splitlines()[0] == "status: destroyed"
    assert _record(ssp_home, tenant_id).status is TenantStatus.DESTROYED


def test_destroy_output_escapes_stored_strings(tmp_path: Path, ssp_home: Path) -> None:
    name = "up\nloads\x1b"
    owner = "caf\u00e9"
    created = _awkward(tmp_path)
    before = _create_real(ssp_home, created, owner=owner)

    result = runner.invoke(app, _destroy("demo", "--yes"))
    lines = result.stdout.splitlines()

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert "\x1b" not in result.stdout
    assert _escape_stored(name) in lines
    assert f"owner: {_escape_stored(owner)}" in lines
    assert before.owner == owner


def test_subprocess_lifecycle_leaves_no_resources_and_keeps_the_destroyed_tenant(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    executable = shutil.which("ssp")
    assert executable is not None
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
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
        ["create", "demo", str(_VALID), "--type", "dev", "--owner", "alice", "--yes"]
    )
    updated = run(["update", "demo", str(added), "--yes"])
    shown = run(["status", "demo"])
    destroyed = run(["destroy", "demo", "--yes"])
    afterwards = run(["status", "demo"])
    listed = run(["tenants"])
    executor_file = ssp_home / "local-executor.sqlite"
    connection = sqlite3.connect(executor_file)
    try:
        tagged = connection.execute(
            "SELECT COUNT(*) FROM resources WHERE tag_tenant_id = ?",
            ("demo",),
        ).fetchone()
        roles = connection.execute(
            "SELECT COUNT(*) FROM tenant_roles WHERE tenant_id = ?",
            ("demo",),
        ).fetchone()
    finally:
        connection.close()
    registry = SqliteRegistry.open_read_only(ssp_home / "tenants.sqlite")
    try:
        records = registry.list_tenants()
    finally:
        registry.close()

    assert created.returncode == 0
    assert "Traceback" not in created.stderr
    assert updated.returncode == 0
    assert "updated: demo status active" in updated.stdout.splitlines()
    assert "Traceback" not in updated.stderr
    assert shown.returncode == 0
    assert "status: active" in shown.stdout.splitlines()
    assert "reports sqs-queue" in shown.stdout.splitlines()
    assert destroyed.returncode == 0
    assert "destroyed: demo status destroyed" in destroyed.stdout.splitlines()
    assert "Traceback" not in destroyed.stderr
    assert afterwards.returncode == 0
    assert afterwards.stdout.splitlines()[0] == "status: destroyed"
    assert listed.returncode == 0
    assert any(
        line.startswith("demo destroyed dev alice ")
        for line in listed.stdout.splitlines()
    )
    assert tagged == (0,)
    assert roles == (0,)
    assert [record.tenant_id for record in records] == ["demo"]
    assert records[0].status is TenantStatus.DESTROYED


def _failure_text(result: Result) -> str:
    text = result.stderr
    if result.exc_info is not None and result.exc_info[1] is not None:
        text += "".join(traceback.format_exception(result.exc_info[1]))
    return text


def _manifest_shape(
    entries: tuple[ManifestEntry, ...] | list[ManifestEntry],
) -> tuple[tuple[str, str, tuple[tuple[str, object], ...]], ...]:
    return tuple(
        (entry.name, entry.type, tuple(sorted(entry.options.items())))
        for entry in entries
    )


def _integrity_ok(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    assert row == ("ok",)


def _spawn(home: Path, args: list[str], cwd: Path) -> subprocess.Popen[str]:
    executable = shutil.which("ssp")
    assert executable is not None
    env = os.environ.copy()
    env["SSP_HOME"] = str(home)
    return subprocess.Popen(
        [executable, *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def test_update_reports_a_lost_conditional_write(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    stack = _Stack(ssp_home, build_catalog(), reject=TenantStatus.UPDATING)
    before = _create_on(stack, _VALID)
    recorded = len(stack.executor.calls)
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)

    result = runner.invoke(stack.cli, _update(added, "--yes"))

    assert result.exit_code == 1
    assert result.stderr == _CHANGED
    assert "Traceback" not in result.stderr
    assert "updated:" not in result.stdout
    assert stack.executor.calls[recorded:] == []
    assert stack.registry.get("demo") == before


def test_destroy_reports_a_lost_conditional_write(ssp_home: Path) -> None:
    stack = _Stack(ssp_home, build_catalog(), reject=TenantStatus.DESTROYING)
    before = _create_on(stack, _VALID)
    recorded = len(stack.executor.calls)

    result = runner.invoke(stack.cli, _destroy("demo", "--yes"))

    assert result.exit_code == 1
    assert result.stderr == _CHANGED
    assert "Traceback" not in result.stderr
    assert "destroyed:" not in result.stdout
    assert stack.executor.calls[recorded:] == []
    assert stack.registry.get("demo") == before


def test_update_does_not_catch_an_unknown_executor_error(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    stack.executor.raise_on_next_provision(KeyError("unknown"))
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)

    result = runner.invoke(stack.cli, _update(added, "--yes"))

    assert result.exit_code != 0
    assert "Traceback" in _failure_text(result)
    assert "KeyError" in _failure_text(result)
    assert _CHANGED.strip() not in result.stderr
    assert stack.registry.get("demo").status is TenantStatus.UPDATING


def test_destroy_does_not_catch_an_unknown_executor_error(ssp_home: Path) -> None:
    stack = _Stack(ssp_home, build_catalog())
    _create_on(stack, _VALID)
    stack.executor.raise_on_next_destroy(KeyError("unknown"))

    result = runner.invoke(stack.cli, _destroy("demo", "--yes"))

    assert result.exit_code != 0
    assert "Traceback" in _failure_text(result)
    assert "KeyError" in _failure_text(result)
    assert _CHANGED.strip() not in result.stderr
    assert stack.registry.get("demo").status is TenantStatus.DESTROYING


def test_concurrent_destroys_of_one_tenant_keep_one_destroyed_tenant(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    # Proves no corruption and no half-applied manifest, not that a race necessarily happened.
    _create_real(ssp_home)
    first = _spawn(ssp_home, ["destroy", "demo", "--yes"], tmp_path)
    second = _spawn(ssp_home, ["destroy", "demo", "--yes"], tmp_path)
    out_first, err_first = first.communicate(timeout=60)
    out_second, err_second = second.communicate(timeout=60)
    winner = out_first if first.returncode == 0 else out_second
    loser_code = second.returncode if first.returncode == 0 else first.returncode
    loser_error = err_second if first.returncode == 0 else err_first

    assert (first.returncode == 0) ^ (second.returncode == 0)
    assert loser_code in {1, 2}
    assert loser_error.strip() != ""
    assert len(loser_error.splitlines()) <= 3
    assert "Traceback" not in err_first
    assert "Traceback" not in err_second
    assert "destroyed: demo status destroyed" in winner.splitlines()
    assert _record(ssp_home).status is TenantStatus.DESTROYED
    _integrity_ok(ssp_home / "tenants.sqlite")
    _integrity_ok(ssp_home / "local-executor.sqlite")


def test_concurrent_updates_leave_one_complete_manifest(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    # Proves no corruption and no half-applied manifest, not that a race necessarily happened.
    _create_real(ssp_home)
    reports = _manifest(
        tmp_path,
        "reports.yaml",
        "- type: s3-bucket\n  name: uploads\n- type: sqs-queue\n  name: reports\n",
    )
    uploads = _manifest(tmp_path, "uploads.yaml", _UPLOADS_ONLY)
    first_shape = _manifest_shape(tuple(load_manifest_from_file(reports).root))
    second_shape = _manifest_shape(tuple(load_manifest_from_file(uploads).root))
    first = _spawn(
        ssp_home,
        ["update", "demo", str(reports), "--yes", "--confirm-removals"],
        tmp_path,
    )
    second = _spawn(
        ssp_home,
        ["update", "demo", str(uploads), "--yes", "--confirm-removals"],
        tmp_path,
    )
    _out_first, err_first = first.communicate(timeout=60)
    _out_second, err_second = second.communicate(timeout=60)
    record = _record(ssp_home)

    assert first_shape != second_shape
    for code, error in (
        (first.returncode, err_first),
        (second.returncode, err_second),
    ):
        assert "Traceback" not in error
        if code != 0:
            assert error.strip() != ""
            assert len(error.splitlines()) <= 3
    assert record.status is TenantStatus.ACTIVE
    assert _manifest_shape(record.manifest) in {first_shape, second_shape}
    _integrity_ok(ssp_home / "tenants.sqlite")
    _integrity_ok(ssp_home / "local-executor.sqlite")


def test_confirm_replacement_option_parses_on_the_installed_command(
    tmp_path: Path,
    ssp_home: Path,
) -> None:
    # The installed catalog has no replacement-forcing option. These names are
    # not replacements. The repeated option must parse, and the update proceeds.
    added = _manifest(tmp_path, "added.yaml", _WITH_REPORTS)
    created = _spawn(
        ssp_home,
        [
            "create",
            "demo",
            str(_VALID),
            "--type",
            "dev",
            "--owner",
            "alice",
            "--yes",
        ],
        tmp_path,
    )
    created_out, created_err = created.communicate(timeout=60)
    updated = _spawn(
        ssp_home,
        [
            "update",
            "demo",
            str(added),
            "--yes",
            "--confirm-replacement",
            "queue-2",
            "--confirm-replacement",
            "bucket-3",
        ],
        tmp_path,
    )
    updated_out, updated_err = updated.communicate(timeout=60)
    record = _record(ssp_home)

    assert created.returncode == 0
    assert "Traceback" not in created_err
    assert "created: demo status active" in created_out.splitlines()
    assert updated.returncode == 0
    assert updated_err == ""
    assert "Traceback" not in updated_out
    assert "updated: demo status active" in updated_out.splitlines()
    assert tuple(entry.name for entry in record.manifest) == (
        "uploads",
        "jobs",
        "reports",
    )

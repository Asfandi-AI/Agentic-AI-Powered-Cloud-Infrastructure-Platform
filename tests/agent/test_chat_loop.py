import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from self_service_platform.agent.loop import run_chat
from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.audit import AuditEntry, Decision, Outcome, ToolName
from self_service_platform.core.chat_host import ChatHost
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.executor import ProvisionRequest, ResourceRef
from self_service_platform.core.fake_model_client import FakeModelClient
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.model_client import (
    ConversationMessage,
    MessageRole,
    ModelResponse,
    ToolCall,
)
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    CreatePlan,
    DestroyPlan,
    PlatformService,
    TtlPolicy,
    UpdateConfirmations,
    UpdatePlan,
)
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
)
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

_NAMES = (
    "list_catalog",
    "draft_manifest",
    "validate_manifest",
    "estimate",
    "plan_create",
    "plan_update",
    "execute_create",
    "execute_update",
    "list_tenants",
    "tenant_status",
    "plan_destroy",
    "execute_destroy",
)
_FIXED = "fixed-confirmation-token"
T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
DEV_TTL = timedelta(days=1)
EPHEMERAL_TTL = timedelta(hours=3)
TENANT_ID = "tenant-a"
_BOTH = (
    {"type": "s3-bucket", "name": "uploads", "options": {}},
    {"type": "sqs-queue", "name": "jobs", "options": {}},
)
_UPLOADS = ({"type": "s3-bucket", "name": "uploads", "options": {}},)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _Audit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _RecordingRegistry:
    def __init__(self, inner: SqliteRegistry, calls: list[str]) -> None:
        self._inner = inner
        self.calls = calls

    def create_tenant(self, record: TenantRecord) -> None:
        self.calls.append("create_tenant")
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
        self.calls.append("transition")
        return self._inner.transition(tenant_id, expected, new, fields)


class _RecordingExecutor:
    def __init__(self, inner: FakeExecutor, calls: list[str]) -> None:
        self._inner = inner
        self.calls = calls

    def provision(self, request: ProvisionRequest) -> None:
        self.calls.append("provision")
        self._inner.provision(request)

    def destroy(self, tenant_id: str) -> None:
        self.calls.append("destroy")
        self._inner.destroy(tenant_id)

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        self.calls.append("list_tagged_resources")
        return self._inner.list_tagged_resources(tenant_id, component)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        return self._inner.tenant_role_exists(tenant_id)


class _Host:
    def __init__(
        self,
        lines: list[str],
        *,
        accept_create: bool = False,
        update: UpdateConfirmations | None = None,
        typed_ids: list[str] | None = None,
    ) -> None:
        self._lines = list(lines)
        self.accept_create = accept_create
        self.update = update
        self.typed_ids = list(typed_ids or [])
        self.published: list[str] = []
        self.created: list[CreatePlan] = []
        self.updated: list[UpdatePlan] = []
        self.destroyed: list[DestroyPlan] = []

    def read_message(self) -> str | None:
        if len(self._lines) == 0:
            return None
        return self._lines.pop(0)

    def publish(self, text: str) -> None:
        self.published.append(text)

    def confirm_create(self, plan: CreatePlan) -> bool:
        self.created.append(plan)
        return self.accept_create

    def confirm_update(self, plan: UpdatePlan) -> UpdateConfirmations | None:
        self.updated.append(plan)
        return self.update

    def confirm_destroy(self, plan: DestroyPlan) -> bool:
        self.destroyed.append(plan)
        typed = self.typed_ids.pop(0)
        return typed == plan.tenant_id


@dataclass
class _World:
    service: PlatformService
    audit: _Audit
    sqlite: SqliteRegistry
    calls: list[str]
    catalog: Catalog
    initiator: Initiator


@contextmanager
def _world(tmp_path: Path) -> Iterator[_World]:
    calls: list[str] = []
    clock = _Clock()
    audit = _Audit()
    issuer, verifier = confirmation_pair(clock)
    catalog = build_catalog()
    initiator = Initiator(kind=InitiatorKind.AGENT, owner="agent")
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        service = PlatformService(
            catalog=catalog,
            registry=_RecordingRegistry(sqlite, calls),
            executor=_RecordingExecutor(FakeExecutor(), calls),
            clock=clock,
            ttl_policy=TtlPolicy(dev=DEV_TTL, ephemeral=EPHEMERAL_TTL),
            audit_log=audit,
            issuer=issuer,
            verifier=verifier,
        )
        yield _World(
            service=service,
            audit=audit,
            sqlite=sqlite,
            calls=calls,
            catalog=catalog,
            initiator=initiator,
        )


def _seed(world: _World) -> None:
    manifest = Manifest.model_validate(list(_BOTH))
    initiator = Initiator(kind=InitiatorKind.CLI, owner="cli")
    world.sqlite.create_tenant(
        TenantRecord(
            tenant_id=TENANT_ID,
            tenant_type=TenantType.DEV,
            status=TenantStatus.CREATING,
            manifest=tuple(manifest.root),
            catalog_version=world.catalog.catalog_version,
            owner="owner",
            initiator=initiator,
            expires_at=T0 + DEV_TTL,
            last_error=None,
            updated_at=T0,
        )
    )
    current = world.sqlite.get(TENANT_ID)
    world.sqlite.transition(
        TENANT_ID,
        TenantStatus.CREATING,
        TenantStatus.ACTIVE,
        TransitionFields(
            manifest=current.manifest,
            catalog_version=current.catalog_version,
            initiator=current.initiator,
            expires_at=current.expires_at,
            last_error=None,
        ),
    )


def _text(text: str) -> ModelResponse:
    return ModelResponse(text=text)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments=arguments),)
    )


def _create_arguments() -> dict[str, object]:
    return {
        "tenant_id": TENANT_ID,
        "tenant_type": "dev",
        "owner": "owner",
        "entries": list(_BOTH),
    }


def _run(world: _World, client: FakeModelClient, host: ChatHost) -> None:
    run_chat(world.service, client, world.initiator, host)


def _tool_messages(client: FakeModelClient, index: int) -> list[ConversationMessage]:
    return [
        message
        for message in client.calls[index].messages
        if message.role is MessageRole.TOOL
    ]


def _dump(audit: _Audit) -> str:
    return json.dumps([entry.model_dump(mode="json") for entry in audit.entries])


def _fix_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "self_service_platform.core.confirmation_issuer.secrets.token_urlsafe",
        lambda _size: _FIXED,
    )


def test_the_model_is_offered_the_twelve_tools_and_a_system_message(
    tmp_path: Path,
) -> None:
    client = FakeModelClient([_text("ready")])
    host = _Host(["list the catalog"])
    with _world(tmp_path) as world:
        _run(world, client, host)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert [tool.name for tool in call.tools] == list(_NAMES)
    assert all(
        not name.startswith("confirm_") and not name.startswith("decline_")
        for name in (tool.name for tool in call.tools)
    )
    assert call.messages[0].role is MessageRole.SYSTEM
    assert "token" not in (call.messages[0].text or "")
    assert call.messages[1].role is MessageRole.USER
    assert call.messages[1].text == "list the catalog"
    assert host.published == ["ready"]


def test_list_catalog_result_is_returned_to_the_model(tmp_path: Path) -> None:
    client = FakeModelClient(
        [
            _call("call-1", "list_catalog", {}),
            _text("The catalog is ready."),
        ]
    )
    host = _Host(["list the catalog"])
    with _world(tmp_path) as world:
        _run(world, client, host)

        assert host.published == ["The catalog is ready."]
        assert world.calls == []
        assert world.sqlite.list_tenants() == ()
        tool_message = _tool_messages(client, 1)[-1]
        assert tool_message.tool_call_id == "call-1"
        payload = json.loads(tool_message.text or "")
        assert payload["catalog_version"] == world.catalog.catalog_version
        assert len(payload["components"]) == len(world.catalog.list_components())


def test_a_confirmed_create_reaches_active_without_printing_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = FakeModelClient(
        [
            _call("plan-1", "plan_create", _create_arguments()),
            _call(
                "execute-1",
                "execute_create",
                {"tenant_id": TENANT_ID, "token": _FIXED},
            ),
            _text("Created tenant-a."),
        ]
    )
    host = _Host(["create tenant-a"], accept_create=True)
    with _world(tmp_path) as world:
        _run(world, client, host)

        plan = host.created[0]
        assert plan.tenant_id == TENANT_ID
        assert plan.estimated_cost.total is None
        assert plan.expires_at == T0 + DEV_TTL
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        confirmed = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.decision is Decision.CONFIRMED
        ]
        succeeded = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.outcome is Outcome.SUCCEEDED
        ]
        assert len(confirmed) == 1
        assert len(succeeded) == 1
        assert _FIXED not in _dump(world.audit)
        assert _FIXED not in "\n".join(host.published)
        assert host.published == ["Created tenant-a."]
        plan_result = _tool_messages(client, 1)[0]
        assert _FIXED in (plan_result.text or "")


def test_a_declined_create_sends_no_token_and_creates_nothing(tmp_path: Path) -> None:
    client = FakeModelClient(
        [
            _call("plan-1", "plan_create", _create_arguments()),
            _text("Nothing was created."),
        ]
    )
    host = _Host(["create tenant-a"], accept_create=False)
    with _world(tmp_path) as world:
        _run(world, client, host)

        declined = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.decision is Decision.DECLINED
        ]
        assert len(declined) == 1
        payload = json.loads(_tool_messages(client, 1)[0].text or "")
        assert payload["confirmed"] is False
        assert "token" not in payload
        assert "create_tenant" not in world.calls
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)
        assert host.published == ["Nothing was created."]


def test_a_removal_update_runs_only_after_the_host_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declined = FakeModelClient(
        [
            _call(
                "plan-1",
                "plan_update",
                {"tenant_id": TENANT_ID, "entries": list(_UPLOADS)},
            ),
            _text("The update was declined."),
        ]
    )
    decline_home = tmp_path / "decline_scenario"
    decline_home.mkdir()
    with _world(decline_home) as world:
        _seed(world)
        called: list[str] = []

        def _refuse(*_args: object, **_kwargs: object) -> None:
            called.append("apply_update")
            raise ConfirmationRequiredError("apply_update")

        world.service.apply_update = _refuse  # type: ignore[method-assign]
        host = _Host(["remove jobs"], update=None)
        _run(world, declined, host)

        assert called == []
        assert {entry.name for entry in world.sqlite.get(TENANT_ID).manifest} == {
            "uploads",
            "jobs",
        }

    _fix_token(monkeypatch)
    accepted = FakeModelClient(
        [
            _call(
                "plan-1",
                "plan_update",
                {"tenant_id": TENANT_ID, "entries": list(_UPLOADS)},
            ),
            _call(
                "execute-1",
                "execute_update",
                {"tenant_id": TENANT_ID, "token": _FIXED},
            ),
            _text("Removed jobs."),
        ]
    )
    accept_home = tmp_path / "accept_scenario"
    accept_home.mkdir()
    with _world(accept_home) as world:
        _seed(world)
        host = _Host(
            ["remove jobs"],
            update=UpdateConfirmations(
                update=True, removals=True, replacements=frozenset()
            ),
        )
        _run(world, accepted, host)

        record = world.sqlite.get(TENANT_ID)
        assert record.status is TenantStatus.ACTIVE
        assert {entry.name for entry in record.manifest} == {"uploads"}
        succeeded = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_UPDATE
            and entry.outcome is Outcome.SUCCEEDED
        ]
        assert len(succeeded) == 1
        assert succeeded[0].arguments["data_loss_confirmed"] is True
        assert succeeded[0].arguments["removal_count"] == 1


def test_typing_the_tenant_id_destroys_the_tenant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = FakeModelClient(
        [
            _call("plan-1", "plan_destroy", {"tenant_id": TENANT_ID}),
            _text("The tenant is still active."),
            _call("plan-2", "plan_destroy", {"tenant_id": TENANT_ID}),
            _call(
                "execute-1",
                "execute_destroy",
                {"tenant_id": TENANT_ID, "token": _FIXED},
            ),
            _text("Destroyed tenant-a."),
        ]
    )
    host = _Host(
        ["destroy tenant-a", "destroy tenant-a again"],
        typed_ids=["nope", TENANT_ID],
    )
    with _world(tmp_path) as world:
        _seed(world)
        _run(world, client, host)

        assert world.sqlite.get(TENANT_ID).status is TenantStatus.DESTROYED
        declined = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.DESTROY
            and entry.decision is Decision.DECLINED
        ]
        succeeded = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.DESTROY
            and entry.outcome is Outcome.SUCCEEDED
        ]
        assert len(declined) == 1
        assert len(succeeded) == 1
        assert succeeded[0].initiator_kind is InitiatorKind.AGENT
        assert _FIXED not in _dump(world.audit)
        assert _FIXED not in "\n".join(host.published)


def test_a_made_up_execute_token_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = FakeModelClient(
        [
            _call("plan-1", "plan_create", _create_arguments()),
            _call(
                "execute-1",
                "execute_create",
                {"tenant_id": TENANT_ID, "token": "made-up"},
            ),
            _text("I could not create it."),
        ]
    )
    host = _Host(["create tenant-a"], accept_create=True)
    with _world(tmp_path) as world:
        _run(world, client, host)

        refused = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.outcome is Outcome.REFUSED
        ]
        assert len(refused) == 1
        assert "unknown confirmation token" in (
            _tool_messages(client, 2)[-1].text or ""
        )
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)
        assert host.published == ["I could not create it."]

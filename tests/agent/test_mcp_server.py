import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from self_service_platform.agent.mcp_server import McpSession
from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.audit import AuditEntry, Decision, Outcome, ToolName
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.executor import ProvisionRequest, ResourceRef
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    PlatformService,
    TtlPolicy,
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
_DESTROY_STATEMENT = "all resources and data of the tenant will be deleted"
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


class _Client:
    def __init__(self, answers: list[dict[str, Any]] | None = None) -> None:
        self.answers = list(answers or [])
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if len(self.answers) == 0:
            return {"action": "decline"}
        return self.answers.pop(0)


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


def _fix_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "self_service_platform.core.confirmation_issuer.secrets.token_urlsafe",
        lambda _size: _FIXED,
    )


def _dump(audit: _Audit) -> str:
    return json.dumps([entry.model_dump(mode="json") for entry in audit.entries])


def _message(
    method: str,
    *,
    request_id: int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


def _open(session: McpSession, *, elicitation: bool) -> None:
    capabilities: dict[str, Any] = {}
    if elicitation:
        capabilities["elicitation"] = {}
    response = session.handle(
        _message(
            "initialize",
            request_id=1,
            params={
                "protocolVersion": "2025-06-18",
                "capabilities": capabilities,
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
    )
    assert response is not None
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert session.handle(_message("notifications/initialized")) is None


def _invoke(
    session: McpSession,
    name: str,
    arguments: dict[str, Any],
    request_id: int = 3,
) -> dict[str, Any]:
    response = session.handle(
        _message(
            "tools/call",
            request_id=request_id,
            params={"name": name, "arguments": arguments},
        )
    )
    assert response is not None
    assert response["id"] == request_id
    return response


def _text(response: dict[str, Any]) -> str:
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    assert isinstance(text, str)
    return text


def _body(response: dict[str, Any]) -> dict[str, Any]:
    loaded = json.loads(_text(response))
    assert isinstance(loaded, dict)
    return loaded


def _create_arguments() -> dict[str, Any]:
    return {
        "tenant_id": TENANT_ID,
        "tenant_type": "dev",
        "owner": "owner",
        "entries": list(_BOTH),
    }


def _session(world: _World, client: _Client) -> McpSession:
    return McpSession(world.service, world.initiator, elicit=client)


def test_tools_list_returns_the_twelve_tools(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        session = _session(world, _Client())
        _open(session, elicitation=True)
        response = session.handle(_message("tools/list", request_id=2, params={}))

        assert response is not None
        names = [tool["name"] for tool in response["result"]["tools"]]
        assert names == list(_NAMES)
        assert all(
            not name.startswith("confirm_") and not name.startswith("decline_")
            for name in names
        )


def test_list_catalog_returns_the_catalog_and_writes_no_tenant(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        session = _session(world, _Client())
        _open(session, elicitation=True)
        response = _invoke(session, "list_catalog", {})

        payload = _body(response)
        assert payload["catalog_version"] == world.catalog.catalog_version
        assert len(payload["components"]) == len(world.catalog.list_components())
        assert world.calls == []
        assert world.sqlite.list_tenants() == ()


def test_an_accepted_create_returns_the_issued_value_and_reaches_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = _Client([{"action": "accept", "content": {"accept": True}}])
    with _world(tmp_path) as world:
        session = _session(world, client)
        _open(session, elicitation=True)
        planned = _invoke(session, "plan_create", _create_arguments())
        created = _invoke(
            session,
            "execute_create",
            {"tenant_id": TENANT_ID, "token": _FIXED},
            request_id=4,
        )

        assert client.requests[0]["method"] == "elicitation/create"
        message = client.requests[0]["params"]["message"]
        assert TENANT_ID in message
        assert "dev" in message
        assert "owner" in message
        assert "not priced" in message
        assert _body(planned)["confirmed"] is True
        assert _body(planned)["token"] == _FIXED
        assert _FIXED not in _text(created)
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        assert _FIXED not in _dump(world.audit)


def test_a_declined_create_records_declined_and_creates_nothing(
    tmp_path: Path,
) -> None:
    client = _Client([{"action": "decline"}])
    with _world(tmp_path) as world:
        session = _session(world, client)
        _open(session, elicitation=True)
        planned = _invoke(session, "plan_create", _create_arguments())

        declined = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.decision is Decision.DECLINED
        ]
        payload = _body(planned)
        assert len(client.requests) == 1
        assert len(declined) == 1
        assert payload["confirmed"] is False
        assert "token" not in payload
        assert "create_tenant" not in world.calls
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)


def test_a_removal_update_runs_only_after_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decline_home = tmp_path / "decline_scenario"
    decline_home.mkdir()
    declined = _Client([{"action": "decline"}])
    with _world(decline_home) as world:
        _seed(world)
        called: list[str] = []

        def _refuse(*_args: object, **_kwargs: object) -> None:
            called.append("apply_update")
            raise ConfirmationRequiredError("apply_update")

        world.service.apply_update = _refuse  # type: ignore[method-assign]
        session = _session(world, declined)
        _open(session, elicitation=True)
        _invoke(
            session,
            "plan_update",
            {"tenant_id": TENANT_ID, "entries": list(_UPLOADS)},
        )

        assert len(declined.requests) == 1
        assert called == []
        assert {entry.name for entry in world.sqlite.get(TENANT_ID).manifest} == {
            "uploads",
            "jobs",
        }

    _fix_token(monkeypatch)
    accept_home = tmp_path / "accept_scenario"
    accept_home.mkdir()
    accepted = _Client(
        [
            {
                "action": "accept",
                "content": {"accept": True, "removals": True, "replacements": []},
            }
        ]
    )
    with _world(accept_home) as world:
        _seed(world)
        session = _session(world, accepted)
        _open(session, elicitation=True)
        _invoke(
            session,
            "plan_update",
            {"tenant_id": TENANT_ID, "entries": list(_UPLOADS)},
        )
        _invoke(
            session,
            "execute_update",
            {"tenant_id": TENANT_ID, "token": _FIXED},
            request_id=4,
        )

        message = accepted.requests[0]["params"]["message"]
        record = world.sqlite.get(TENANT_ID)
        succeeded = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_UPDATE
            and entry.outcome is Outcome.SUCCEEDED
        ]
        assert len(accepted.requests) == 1
        assert TENANT_ID in message
        assert "jobs" in message
        assert record.status is TenantStatus.ACTIVE
        assert {entry.name for entry in record.manifest} == {"uploads"}
        assert len(succeeded) == 1
        assert succeeded[0].arguments["data_loss_confirmed"] is True
        assert succeeded[0].arguments["removal_count"] == 1


def test_a_typed_destroy_id_must_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = _Client(
        [
            {"action": "accept", "content": {"tenant_id": "nope"}},
            {"action": "accept", "content": {"tenant_id": TENANT_ID}},
        ]
    )
    with _world(tmp_path) as world:
        _seed(world)
        session = _session(world, client)
        _open(session, elicitation=True)
        _invoke(session, "plan_destroy", {"tenant_id": TENANT_ID})

        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        _invoke(session, "plan_destroy", {"tenant_id": TENANT_ID}, request_id=4)
        _invoke(
            session,
            "execute_destroy",
            {"tenant_id": TENANT_ID, "token": _FIXED},
            request_id=5,
        )

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
        assert [item["method"] for item in client.requests] == [
            "elicitation/create",
            "elicitation/create",
        ]
        assert _DESTROY_STATEMENT in client.requests[1]["params"]["message"]
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.DESTROYED
        assert len(declined) == 1
        assert len(succeeded) == 1
        assert succeeded[0].initiator_kind is InitiatorKind.AGENT
        assert _FIXED not in _dump(world.audit)


def test_a_made_up_execute_value_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = _Client([{"action": "accept", "content": {"accept": True}}])
    with _world(tmp_path) as world:
        session = _session(world, client)
        _open(session, elicitation=True)
        _invoke(session, "plan_create", _create_arguments())
        refused_call = _invoke(
            session,
            "execute_create",
            {"tenant_id": TENANT_ID, "token": "made-up"},
            request_id=4,
        )

        refused = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.outcome is Outcome.REFUSED
        ]
        assert len(refused) == 1
        assert refused_call["result"]["isError"] is True
        assert "unknown confirmation token" in _text(refused_call)
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)


def test_a_client_without_elicitation_gets_no_issued_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_token(monkeypatch)
    client = _Client([{"action": "accept", "content": {"accept": True}}])
    with _world(tmp_path) as world:
        session = _session(world, client)
        _open(session, elicitation=False)
        planned = _invoke(session, "plan_create", _create_arguments())

        payload = _body(planned)
        assert client.requests == []
        assert payload["confirmed"] is False
        assert "token" not in payload
        assert _FIXED not in _text(planned)


def test_a_rejected_plan_sends_no_elicitation(tmp_path: Path) -> None:
    client = _Client([{"action": "accept", "content": {"accept": True}}])
    with _world(tmp_path) as world:
        session = _session(world, client)
        _open(session, elicitation=True)
        response = _invoke(
            session,
            "plan_create",
            {
                "tenant_id": TENANT_ID,
                "tenant_type": "dev",
                "owner": "owner",
                "entries": [
                    {"type": "not-a-component", "name": "bad", "options": {}},
                ],
            },
        )

        assert client.requests == []
        assert response["result"]["isError"] is True
        assert world.calls == []
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)


def test_an_unknown_tool_does_not_change_the_service(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        session = _session(world, _Client())
        _open(session, elicitation=True)
        response = _invoke(session, "confirm_create", {"tenant_id": TENANT_ID})

        assert response["result"]["isError"] is True
        assert world.calls == []
        assert world.sqlite.list_tenants() == ()

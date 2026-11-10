import ast
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from self_service_platform.agent import AgentTools
from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.audit import AuditEntry, Decision, Outcome, ToolName
from self_service_platform.core.confirmation import (
    TokenAlreadyUsedError,
    UnknownTokenError,
)
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.executor import ProvisionRequest, ResourceRef
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.model_client import ToolSpec
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    PlatformService,
    TtlPolicy,
    UpdateConfirmations,
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
from self_service_platform.core.validator import (
    ManifestValidationError,
    validate_manifest,
)
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

_ROOT = Path(__file__).resolve().parents[2]
_AGENT = _ROOT / "src" / "self_service_platform" / "agent"
_FORBIDDEN = (
    "boto3",
    "botocore",
    "aws_cdk",
    "self_service_platform.executors",
    "self_service_platform.registry",
)
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
_DESTROY_STATEMENT = "all resources and data of the tenant will be deleted"
T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
DEV_TTL = timedelta(days=1)
EPHEMERAL_TTL = timedelta(hours=3)
TENANT_ID = "tenant-a"
_BOTH = (("s3-bucket", "uploads"), ("sqs-queue", "jobs"))
_UPLOADS = (("s3-bucket", "uploads"),)


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


@dataclass
class _World:
    service: PlatformService
    audit: _Audit
    sqlite: SqliteRegistry
    calls: list[str]
    catalog: Catalog
    agent: AgentTools
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
            agent=AgentTools(initiator),
            initiator=initiator,
        )


def _manifest(entries: tuple[tuple[str, str], ...]) -> Manifest:
    return Manifest.model_validate(_entry_dicts(entries))


def _entry_dicts(entries: tuple[tuple[str, str], ...]) -> list[dict[str, object]]:
    return [
        {"type": component_type, "name": name, "options": {}}
        for component_type, name in entries
    ]


def _tool(agent: AgentTools, name: str) -> Any:
    matches = [tool for tool in agent.tools() if tool.name == name]
    assert len(matches) == 1
    return matches[0]


def _seed(world: _World, entries: tuple[tuple[str, str], ...] = _BOTH) -> None:
    manifest = _manifest(entries)
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


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        found: list[str] = []
        for item in value.values():
            found.extend(_strings(item))
        return found
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        found = []
        for item in value:
            found.extend(_strings(item))
        return found
    return []


def _keys(value: object) -> list[str]:
    if isinstance(value, Mapping):
        found = [str(key) for key in value]
        for item in value.values():
            found.extend(_keys(item))
        return found
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        found = []
        for item in value:
            found.extend(_keys(item))
        return found
    return []


def _succeeded(audit: _Audit, tool: ToolName) -> list[AuditEntry]:
    return [
        entry
        for entry in audit.entries
        if entry.tool_name is tool and entry.outcome is Outcome.SUCCEEDED
    ]


def _refused(audit: _Audit, tool: ToolName) -> list[AuditEntry]:
    return [
        entry
        for entry in audit.entries
        if entry.tool_name is tool and entry.outcome is Outcome.REFUSED
    ]


def _dump(audit: _Audit) -> str:
    return json.dumps([entry.model_dump(mode="json") for entry in audit.entries])


def test_agent_tools_exposes_exactly_twelve_schemas() -> None:
    agent = AgentTools(Initiator(kind=InitiatorKind.AGENT, owner="agent"))
    tools = list(agent.tools())
    names = [tool.name for tool in tools]

    assert names == list(_NAMES)
    assert len(names) == len(set(names)) == 12
    assert all(
        not name.startswith("confirm_") and not name.startswith("decline_")
        for name in names
    )
    for tool in tools:
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert tool.parameters["additionalProperties"] is False
        assert isinstance(tool.parameters["properties"], dict)
        spec = ToolSpec(
            name=tool.name,
            description=tool.description,
            parameters=dict(tool.parameters),
        )
        assert spec.name == tool.name


def test_read_tools_do_not_mutate_and_write_one_succeeded_entry(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        _seed(world)
        before = len(world.calls)

        catalog_result = _tool(world.agent, "list_catalog").execute(world.service, {})
        listed = {item["component_type"]: item for item in catalog_result["components"]}
        expected = {
            component.component_type: component
            for component in world.catalog.list_components()
        }
        assert set(listed) == set(expected)
        for component_type, component in expected.items():
            assert listed[component_type]["description"] == component.description
            assert listed[component_type]["service"] == component.aws_service.value
        assert catalog_result["catalog_version"] == world.catalog.catalog_version

        tenants = _tool(world.agent, "list_tenants").execute(world.service, {})
        assert tenants["tenants"] == [
            {
                "tenant_id": TENANT_ID,
                "status": "active",
                "tenant_type": "dev",
            }
        ]

        status = _tool(world.agent, "tenant_status").execute(
            world.service, {"tenant_id": TENANT_ID}
        )
        assert status["tenant_id"] == TENANT_ID
        assert status["status"] == "active"
        assert status["tenant_type"] == "dev"
        assert status["owner"] == "owner"
        assert status["catalog_version"] == world.catalog.catalog_version
        assert status["component_names"] == ["uploads", "jobs"]

        assert world.calls[before:] == []
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        for tool_name in (
            ToolName.LIST_CATALOG,
            ToolName.LIST_TENANTS,
            ToolName.TENANT_STATUS,
        ):
            added = _succeeded(world.audit, tool_name)
            assert len(added) == 1
            assert added[0].initiator_kind is InitiatorKind.AGENT
        assert len(world.audit.entries) == 3


def test_draft_and_validate_reject_an_unknown_component_type(
    tmp_path: Path,
) -> None:
    bad = [{"type": "no-such-type", "name": "uploads", "options": {}}]
    with _world(tmp_path) as world:
        with pytest.raises(ManifestValidationError) as caught:
            validate_manifest(Manifest.model_validate(bad), world.catalog)
        issue = caught.value.issues[0]
        cli_line = f"position {issue.position} {issue.name}: {issue.message}"
        assert cli_line == ("position 1 uploads: unknown component type no-such-type")
        expected = [
            {
                "position": item.position,
                "name": item.name,
                "message": item.message,
            }
            for item in caught.value.issues
        ]

        for tool_name in (ToolName.DRAFT_MANIFEST, ToolName.VALIDATE_MANIFEST):
            result = _tool(world.agent, tool_name.value).execute(
                world.service, {"entries": bad}
            )
            assert result["issues"] == expected
            assert len(_refused(world.audit, tool_name)) == 1

        assert world.calls == []
        assert world.sqlite.list_tenants() == ()


def test_estimate_of_a_valid_manifest_is_not_priced(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        result = _tool(world.agent, "estimate").execute(
            world.service, {"entries": _entry_dicts(_UPLOADS)}
        )

        assert result["total"] is None
        assert result["label"] == "POC heuristic"
        assert "not priced" in _strings(result)
        assert len(_succeeded(world.audit, ToolName.ESTIMATE)) == 1
        assert world.calls == []


def test_plan_create_returns_the_plan_summary_without_a_token(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        result = _tool(world.agent, "plan_create").execute(
            world.service,
            {
                "tenant_id": TENANT_ID,
                "tenant_type": "dev",
                "owner": "owner",
                "entries": _entry_dicts(_BOTH),
            },
        )

        assert result["catalog_version"] == world.catalog.catalog_version
        assert result["expires_at"] == (T0 + DEV_TTL).isoformat()
        assert result["entry_count"] == 2
        assert result["estimated_cost"]["total"] is None
        assert result["estimated_cost"]["label"] == "POC heuristic"
        assert "token" not in _keys(result)
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)
        planned = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.PLAN_CREATE
        ]
        assert len(planned) == 1
        assert planned[0].outcome is Outcome.SUCCEEDED
        assert planned[0].initiator_kind is InitiatorKind.AGENT


def test_execute_create_with_an_empty_token_is_refused(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        _tool(world.agent, "plan_create").execute(
            world.service,
            {
                "tenant_id": TENANT_ID,
                "tenant_type": "dev",
                "owner": "owner",
                "entries": _entry_dicts(_BOTH),
            },
        )

        with pytest.raises(UnknownTokenError):
            _tool(world.agent, "execute_create").execute(
                world.service, {"tenant_id": TENANT_ID, "token": ""}
            )

        assert len(_refused(world.audit, ToolName.EXECUTE_CREATE)) == 1
        with pytest.raises(TenantNotFoundError):
            world.sqlite.get(TENANT_ID)


def test_execute_create_after_confirm_reaches_active_once(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        _tool(world.agent, "plan_create").execute(
            world.service,
            {
                "tenant_id": TENANT_ID,
                "tenant_type": "dev",
                "owner": "owner",
                "entries": _entry_dicts(_BOTH),
            },
        )
        plan = world.agent.remembered_plan(TENANT_ID)
        token = world.service.confirm_create(plan, world.initiator)

        result = _tool(world.agent, "execute_create").execute(
            world.service, {"tenant_id": TENANT_ID, "token": token}
        )

        assert result["tenant_id"] == TENANT_ID
        assert result["status"] == "active"
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        confirmed = [
            entry
            for entry in world.audit.entries
            if entry.tool_name is ToolName.EXECUTE_CREATE
            and entry.decision is Decision.CONFIRMED
        ]
        assert len(confirmed) == 1
        assert len(_succeeded(world.audit, ToolName.EXECUTE_CREATE)) == 1
        assert token not in _dump(world.audit)
        assert token not in json.dumps(result)

        with pytest.raises(TokenAlreadyUsedError):
            _tool(world.agent, "execute_create").execute(
                world.service, {"tenant_id": TENANT_ID, "token": token}
            )
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE


def test_execute_update_removes_a_component_only_after_the_host_accepts(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        _seed(world)
        _tool(world.agent, "plan_update").execute(
            world.service,
            {"tenant_id": TENANT_ID, "entries": _entry_dicts(_UPLOADS)},
        )
        execute = _tool(world.agent, "execute_update")
        original = world.service.apply_update

        def _called(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("apply_update")

        world.service.apply_update = _called  # type: ignore[method-assign]
        try:
            with pytest.raises(ConfirmationRequiredError):
                execute.execute(
                    world.service, {"tenant_id": TENANT_ID, "token": "not-issued"}
                )
            with pytest.raises(Exception) as caught:
                execute.execute(
                    world.service,
                    {
                        "tenant_id": TENANT_ID,
                        "token": "not-issued",
                        "removals": True,
                        "replacements": ["jobs"],
                        "data_loss_confirmed": True,
                    },
                )
            assert not isinstance(caught.value, AssertionError)
        finally:
            world.service.apply_update = original  # type: ignore[method-assign]

        assert {entry.name for entry in world.sqlite.get(TENANT_ID).manifest} == {
            "uploads",
            "jobs",
        }
        plan = world.agent.remembered_plan(TENANT_ID)
        world.agent.accept_update(
            TENANT_ID,
            UpdateConfirmations(update=True, removals=True, replacements=frozenset()),
        )
        token = world.service.confirm_update(
            plan,
            world.initiator,
            data_loss_confirmed=True,
            confirmed_replacements=frozenset(),
        )

        result = execute.execute(
            world.service, {"tenant_id": TENANT_ID, "token": token}
        )

        assert result["status"] == "active"
        record = world.sqlite.get(TENANT_ID)
        assert record.status is TenantStatus.ACTIVE
        assert {entry.name for entry in record.manifest} == {"uploads"}
        succeeded = _succeeded(world.audit, ToolName.EXECUTE_UPDATE)
        assert len(succeeded) == 1
        assert succeeded[0].arguments["data_loss_confirmed"] is True
        assert succeeded[0].arguments["removal_count"] == 1
        assert token not in _dump(world.audit)


def test_plan_destroy_leaves_the_tenant_active(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        _seed(world)

        result = _tool(world.agent, "plan_destroy").execute(
            world.service, {"tenant_id": TENANT_ID}
        )

        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        assert list(result["component_names"]) == ["uploads", "jobs"]
        assert result["statement"] == _DESTROY_STATEMENT
        assert result["status"] == "active"
        assert result["tenant_type"] == "dev"
        assert "token" not in _keys(result)
        planned = [
            entry for entry in world.audit.entries if entry.tool_name == "plan_destroy"
        ]
        assert len(planned) == 1
        assert planned[0].outcome is Outcome.SUCCEEDED
        assert planned[0].decision is None
        assert planned[0].initiator_kind is InitiatorKind.AGENT
        assert _succeeded(world.audit, ToolName.DESTROY) == []


def test_execute_destroy_requires_a_confirmed_token(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        _seed(world)
        _tool(world.agent, "plan_destroy").execute(
            world.service, {"tenant_id": TENANT_ID}
        )

        with pytest.raises(UnknownTokenError):
            _tool(world.agent, "execute_destroy").execute(
                world.service, {"tenant_id": TENANT_ID, "token": "not-a-token"}
            )

        assert world.sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        assert len(_refused(world.audit, ToolName.DESTROY)) == 1

        plan = world.agent.remembered_plan(TENANT_ID)
        token = world.service.confirm_destroy(plan, world.initiator)
        result = _tool(world.agent, "execute_destroy").execute(
            world.service, {"tenant_id": TENANT_ID, "token": token}
        )

        assert result["status"] == "destroyed"
        assert world.sqlite.get(TENANT_ID).status is TenantStatus.DESTROYED
        succeeded = _succeeded(world.audit, ToolName.DESTROY)
        assert len(succeeded) == 1
        assert succeeded[0].tool_name is ToolName.DESTROY
        assert succeeded[0].initiator_kind is InitiatorKind.AGENT
        assert token not in _dump(world.audit)
        assert token not in json.dumps(result)


def test_agent_sources_do_not_import_cloud_sdks_or_adapters() -> None:
    paths = sorted(path for path in _AGENT.rglob("*.py") if path.is_file())
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in _FORBIDDEN
                )

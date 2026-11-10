import getpass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from self_service_platform.agent.loop import run_chat
from self_service_platform.app import app
from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import create_app
from self_service_platform.core.chat_host import ChatHost
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.fake_model_client import FakeModelClient
from self_service_platform.core.model_client import ModelResponse, ToolCall
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.tenant import Initiator, InitiatorKind, TenantStatus
from self_service_platform.executors.fake import FakeExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

runner = CliRunner()
_FIXED = "fixed-confirmation-token"
TENANT_ID = "tenant-a"
_ENTRIES = (
    {"type": "s3-bucket", "name": "uploads", "options": {}},
    {"type": "sqs-queue", "name": "jobs", "options": {}},
)


def _text(text: str) -> ModelResponse:
    return ModelResponse(text=text)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments=arguments),)
    )


@pytest.fixture(autouse=True)
def ssp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("SSP_HOME", str(home))
    return home


def _terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "self_service_platform.cli.main._stdin_is_terminal",
        lambda: True,
    )


def _user(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(getpass, "getuser", lambda: name)


def test_chat_without_a_model_client_exits_2(ssp_home: Path) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert result.stderr == "no model client is configured\n"
    assert result.stdout == ""
    assert not ssp_home.exists()


def test_chat_confirms_a_create_and_hides_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch)
    _user(monkeypatch, "owner")
    monkeypatch.setattr(
        "self_service_platform.core.confirmation_issuer.secrets.token_urlsafe",
        lambda _size: _FIXED,
    )

    def clock() -> datetime:
        return datetime(2026, 10, 16, 9, 0, tzinfo=UTC)

    client = FakeModelClient(
        [
            _call(
                "plan-1",
                "plan_create",
                {
                    "tenant_id": TENANT_ID,
                    "tenant_type": "dev",
                    "owner": "owner",
                    "entries": list(_ENTRIES),
                },
            ),
            _call(
                "execute-1",
                "execute_create",
                {"tenant_id": TENANT_ID, "token": _FIXED},
            ),
            _text("Created tenant-a."),
        ]
    )
    seen: list[Initiator] = []

    def chat_runner(
        service: PlatformService, host: ChatHost, initiator: Initiator
    ) -> None:
        seen.append(initiator)
        run_chat(service, client, initiator, host)

    issuer, verifier = confirmation_pair(clock)
    with SqliteRegistry(tmp_path / "tenants.sqlite", clock) as sqlite:
        service = PlatformService(
            catalog=build_catalog(),
            registry=sqlite,
            executor=FakeExecutor(),
            clock=clock,
            ttl_policy=TtlPolicy(dev=timedelta(days=1), ephemeral=timedelta(hours=3)),
            audit_log=_MemoryAudit(),
            issuer=issuer,
            verifier=verifier,
        )
        cli = create_app(
            build_catalog(),
            open_service=lambda: service,
            chat_runner=chat_runner,
        )

        result = runner.invoke(cli, ["chat"], input="please create tenant-a\ny\n")

        assert result.exit_code == 0
        assert "tenant_id: tenant-a" in result.stdout
        assert "tenant_type: dev" in result.stdout
        assert "owner: owner" in result.stdout
        assert "not priced" in result.stdout
        assert "expires_at:" in result.stdout
        assert "Create this tenant? [y/N]" in result.stdout
        assert "Created tenant-a." in result.stdout
        assert _FIXED not in result.stdout
        assert sqlite.get(TENANT_ID).status is TenantStatus.ACTIVE
        assert seen[0].kind is InitiatorKind.AGENT
        assert seen[0].owner == "owner"


class _MemoryAudit:
    def __init__(self) -> None:
        self.entries: list[object] = []

    def write(self, entry: object) -> None:
        self.entries.append(entry)

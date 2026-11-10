"""Bedrock Converse client. These tests do not call AWS."""

import ast
import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from typer.testing import CliRunner

from self_service_platform.core.model_client import (
    ConversationMessage,
    MessageRole,
    ModelClientError,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

_MODEL_ID = "us.anthropic.claude-sonnet-5"
_REGION = "us-east-1"
_MAX_TOKENS = 4096
_SECRET = "aws-secret-value"
_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "src" / "self_service_platform"
_MODEL_PACKAGE = ("self_service_platform.model",)
runner = CliRunner()


class FakeBedrockRuntime:
    """Records converse calls. It does not touch the network."""

    def __init__(
        self,
        response: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response: Mapping[str, Any] = {} if response is None else response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def converse(
        self,
        *,
        modelId: str,
        messages: Sequence[Mapping[str, Any]],
        system: Sequence[Mapping[str, Any]] | None = None,
        toolConfig: Mapping[str, Any] | None = None,
        inferenceConfig: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        recorded: dict[str, Any] = {"modelId": modelId, "messages": messages}
        if system is not None:
            recorded["system"] = system
        if toolConfig is not None:
            recorded["toolConfig"] = toolConfig
        if inferenceConfig is not None:
            recorded["inferenceConfig"] = inferenceConfig
        self.calls.append(copy.deepcopy(recorded))
        if self.error is not None:
            raise self.error
        return self.response


def test_complete_sends_system_user_model_id_and_max_tokens() -> None:
    runtime = FakeBedrockRuntime(response=_text_response("Done."))
    client = _new_client(runtime)
    assert runtime.calls == []
    assert client.model_id == _MODEL_ID
    assert client.region == _REGION

    result = client.complete(
        (
            ConversationMessage(role=MessageRole.SYSTEM, text="Be brief."),
            ConversationMessage(role=MessageRole.USER, text="Hello"),
        ),
        (),
    )

    assert result == ModelResponse(text="Done.")
    assert runtime.calls == [
        {
            "modelId": _MODEL_ID,
            "system": [{"text": "Be brief."}],
            "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
    ]


def test_tool_specs_are_sent_and_empty_tools_omit_tool_config() -> None:
    runtime = FakeBedrockRuntime(response=_text_response("ok"))
    client = _new_client(runtime)
    parameters = {
        "type": "object",
        "properties": {"tenant_id": {"type": "string"}},
        "required": ["tenant_id"],
        "additionalProperties": False,
    }
    spec = ToolSpec(
        name="list_catalog",
        description="List components",
        parameters=parameters,
    )

    client.complete((_user("list"),), (spec,))

    assert runtime.calls == [
        {
            "modelId": _MODEL_ID,
            "messages": [{"role": "user", "content": [{"text": "list"}]}],
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": "list_catalog",
                            "description": "List components",
                            "inputSchema": {"json": parameters},
                        }
                    }
                ]
            },
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
    ]

    runtime.calls.clear()
    client.complete((_user("again"),), ())
    assert runtime.calls == [
        {
            "modelId": _MODEL_ID,
            "messages": [{"role": "user", "content": [{"text": "again"}]}],
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
    ]


def test_tool_results_keep_the_call_id_and_share_one_user_message() -> None:
    runtime = FakeBedrockRuntime(response=_text_response("Done."))
    client = _new_client(runtime)

    client.complete(
        (
            ConversationMessage(role=MessageRole.USER, text="create"),
            ConversationMessage(
                role=MessageRole.ASSISTANT,
                text="I will plan it.",
                tool_calls=(
                    ToolCall(
                        call_id="plan-1",
                        name="plan_create",
                        arguments={"tenant_id": "tenant-a"},
                    ),
                    ToolCall(call_id="plan-2", name="estimate", arguments={}),
                ),
            ),
            ConversationMessage(
                role=MessageRole.TOOL,
                text="planned",
                tool_call_id="plan-1",
            ),
            ConversationMessage(
                role=MessageRole.TOOL,
                text="priced",
                tool_call_id="plan-2",
            ),
        ),
        (),
    )

    assert runtime.calls[0]["messages"] == [
        {"role": "user", "content": [{"text": "create"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "I will plan it."},
                {
                    "toolUse": {
                        "toolUseId": "plan-1",
                        "name": "plan_create",
                        "input": {"tenant_id": "tenant-a"},
                    }
                },
                {
                    "toolUse": {
                        "toolUseId": "plan-2",
                        "name": "estimate",
                        "input": {},
                    }
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "plan-1",
                        "content": [{"text": "planned"}],
                    }
                },
                {
                    "toolResult": {
                        "toolUseId": "plan-2",
                        "content": [{"text": "priced"}],
                    }
                },
            ],
        },
    ]


def test_text_tool_use_and_both_map_to_model_response() -> None:
    assert _complete(
        _message([{"text": "Hello "}, {"text": "there"}])
    ) == ModelResponse(text="Hello there")
    assert _complete(
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "call-1",
                        "name": "list_catalog",
                        "input": {},
                    }
                }
            ],
            stop_reason="tool_use",
        )
    ) == ModelResponse(
        tool_calls=(ToolCall(call_id="call-1", name="list_catalog", arguments={}),)
    )
    assert _complete(
        _message(
            [
                {"reasoningContent": {"reasoningText": {"text": "hidden"}}},
                {"text": "Checking."},
                {
                    "toolUse": {
                        "toolUseId": "call-9",
                        "name": "estimate",
                        "input": {"tenant_id": "tenant-a"},
                    }
                },
            ],
            stop_reason="tool_use",
        )
    ) == ModelResponse(
        text="Checking.",
        tool_calls=(
            ToolCall(
                call_id="call-9",
                name="estimate",
                arguments={"tenant_id": "tenant-a"},
            ),
        ),
    )


def test_empty_content_bad_tool_input_and_transport_errors_raise() -> None:
    user = (_user("go"),)
    empty = FakeBedrockRuntime(response=_message([]))
    with pytest.raises(ModelClientError, match=r"^the model returned no answer$"):
        _new_client(empty).complete(user, ())

    bad_input = FakeBedrockRuntime(
        response=_message(
            [
                {
                    "toolUse": {
                        "toolUseId": "call-1",
                        "name": "list_catalog",
                        "input": "tenant-a",
                    }
                }
            ],
            stop_reason="tool_use",
        )
    )
    with pytest.raises(ModelClientError, match=r"^the model returned no answer$"):
        _new_client(bad_input).complete(user, ())

    denied = FakeBedrockRuntime(error=_client_error(_SECRET))
    with pytest.raises(ModelClientError, match=r"^the model request failed$") as raised:
        _new_client(denied).complete(user, ())
    assert _SECRET not in str(raised.value)

    offline = FakeBedrockRuntime(error=BotoCoreError())
    with pytest.raises(ModelClientError, match=r"^the model request failed$"):
        _new_client(offline).complete(user, ())


def test_wiring_uses_bedrock_only_when_the_model_id_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3
    from self_service_platform.model.bedrock import BedrockModelClient

    from self_service_platform.core.fake_model_client import FakeModelClient
    from self_service_platform.wiring import build_model_client

    calls: list[tuple[str, str]] = []
    created: list[FakeBedrockRuntime] = []

    def fake_client(service_name: str, *, region_name: str) -> FakeBedrockRuntime:
        calls.append((service_name, region_name))
        runtime = FakeBedrockRuntime()
        created.append(runtime)
        return runtime

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.delenv("SSP_BEDROCK_REGION", raising=False)
    monkeypatch.setenv("SSP_BEDROCK_MODEL_ID", "example.model")

    client = build_model_client()
    assert isinstance(client, BedrockModelClient)
    assert client.model_id == "example.model"
    assert client.region == "us-east-1"
    assert calls == [("bedrock-runtime", "us-east-1")]
    assert created[0].calls == []

    calls.clear()
    monkeypatch.setenv("SSP_BEDROCK_REGION", "eu-west-1")
    client = build_model_client()
    assert isinstance(client, BedrockModelClient)
    assert client.model_id == "example.model"
    assert client.region == "eu-west-1"
    assert calls == [("bedrock-runtime", "eu-west-1")]

    calls.clear()
    monkeypatch.setenv("SSP_BEDROCK_REGION", "   ")
    client = build_model_client()
    assert isinstance(client, BedrockModelClient)
    assert client.region == "us-east-1"
    assert calls == [("bedrock-runtime", "us-east-1")]

    def assert_fake() -> None:
        calls.clear()
        built = build_model_client()
        assert isinstance(built, FakeModelClient)
        assert built.calls == []
        assert calls == []

    monkeypatch.setenv("SSP_BEDROCK_REGION", "eu-west-1")
    monkeypatch.delenv("SSP_BEDROCK_MODEL_ID")
    assert_fake()
    monkeypatch.setenv("SSP_BEDROCK_MODEL_ID", "")
    assert_fake()
    monkeypatch.setenv("SSP_BEDROCK_MODEL_ID", "   ")
    assert_fake()


def test_chat_uses_bedrock_only_when_the_model_id_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    from self_service_platform.app import app

    home = tmp_path / "state"
    calls: list[tuple[str, str]] = []
    seen: list[object] = []

    def fake_client(service_name: str, *, region_name: str) -> object:
        calls.append((service_name, region_name))
        return object()

    def capture(
        service: object,
        client: object,
        initiator: object,
        host: object,
    ) -> None:
        del service, initiator, host
        seen.append(client)

    monkeypatch.setenv("SSP_HOME", str(home))
    monkeypatch.delenv("SSP_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("SSP_BEDROCK_REGION", raising=False)
    monkeypatch.delenv("SSP_AWS_REGION", raising=False)
    monkeypatch.delenv("SSP_LOG_GROUP", raising=False)
    monkeypatch.setattr(boto3, "client", fake_client)

    unconfigured = runner.invoke(app, ["chat"])

    assert unconfigured.exit_code == 2
    assert unconfigured.stderr == "no model client is configured\n"
    assert unconfigured.stdout == ""
    assert calls == []
    assert not home.exists()

    from self_service_platform.model.bedrock import BedrockModelClient

    monkeypatch.setattr("self_service_platform.wiring.run_chat", capture)
    monkeypatch.setenv("SSP_BEDROCK_MODEL_ID", _MODEL_ID)
    configured = runner.invoke(app, ["chat"], input="hello\n")

    assert configured.exit_code == 0
    assert calls == [("bedrock-runtime", "us-east-1")]
    assert len(seen) == 1
    assert isinstance(seen[0], BedrockModelClient)


def test_only_wiring_may_import_the_model_package() -> None:
    assert _model_violations() == []

    probe = _PACKAGE / "agent" / "_architecture_model_boundary.py"
    probe.write_text(
        "import self_service_platform.model.bedrock\n",
        encoding="utf-8",
    )
    try:
        violations = _model_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/agent/_architecture_model_boundary.py: "
            "import self_service_platform.model.bedrock"
        )
    ]


def _new_client(
    runtime: FakeBedrockRuntime,
    *,
    model_id: str = _MODEL_ID,
    region: str = _REGION,
) -> Any:
    from self_service_platform.model.bedrock import BedrockModelClient

    return BedrockModelClient(runtime, model_id=model_id, region=region)


def _complete(response: Mapping[str, Any]) -> ModelResponse:
    result = _new_client(FakeBedrockRuntime(response=response)).complete(
        (_user("go"),),
        (),
    )
    assert isinstance(result, ModelResponse)
    return result


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role=MessageRole.USER, text=text)


def _text_response(text: str) -> dict[str, Any]:
    return _message([{"text": text}])


def _message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
    }


def _client_error(message: str) -> ClientError:
    response: dict[str, object] = {
        "Error": {"Code": "AccessDeniedException", "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": 400},
    }
    return ClientError(response, "Converse")


def _model_violations(paths: list[Path] | None = None) -> list[str]:
    chosen = _python_files(_PACKAGE) if paths is None else paths
    found: list[str] = []
    for path in chosen:
        if _model_import_allowed(path):
            continue
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            names = _imported_names(statement)
            if any(_is_named_module(name, _MODEL_PACKAGE) for name in names):
                found.append(f"{relative}: {statement}")
    return found


def _model_import_allowed(path: Path) -> bool:
    relative = path.relative_to(_PACKAGE)
    if relative.as_posix() == "wiring.py":
        return True
    return relative.parts[0] == "model"


def _python_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def _import_statements(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                statements.append(segment)
    return statements


def _imported_names(statement: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(statement)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _is_named_module(name: str, modules: tuple[str, ...]) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in modules)

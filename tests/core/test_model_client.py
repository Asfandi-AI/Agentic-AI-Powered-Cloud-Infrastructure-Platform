import ast
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from self_service_platform.core.fake_model_client import FakeModelClient
from self_service_platform.core.model_client import (
    ConversationMessage,
    MessageRole,
    ModelClient,
    ModelClientError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATHS = (
    _ROOT / "src" / "self_service_platform" / "core" / "model_client.py",
    _ROOT / "src" / "self_service_platform" / "core" / "fake_model_client.py",
)
_FORBIDDEN = ("boto3", "botocore", "socket", "http.client", "urllib")


def _accept(client: ModelClient) -> ModelResponse:
    return client.complete((), ())


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role=MessageRole.USER, text=text)


def _spec() -> ToolSpec:
    return ToolSpec(
        name="list_catalog",
        description="List the catalog",
        parameters={},
    )


def _call() -> ToolCall:
    return ToolCall(
        call_id="call-1",
        name="list_catalog",
        arguments={"tenant_id": "demo"},
    )


def _imported_names(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            names.append(node.module)
    return names


def _forbidden(name: str) -> bool:
    return any(name == item or name.startswith(f"{item}.") for item in _FORBIDDEN)


def test_a_fake_client_satisfies_the_protocol() -> None:
    response = ModelResponse(text="ready")

    assert _accept(FakeModelClient((response,))) == response


def test_scripted_turns_come_back_in_order() -> None:
    text_turn = ModelResponse(text="ready")
    tool_turn = ModelResponse(tool_calls=(_call(),))
    client = FakeModelClient((text_turn, tool_turn))

    assert client.complete((), ()) == text_turn
    assert client.complete((), ()) == tool_turn


def test_calls_record_the_messages_and_the_tool_specs() -> None:
    spec = _spec()
    user = _user("list the catalog")
    assistant = ConversationMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=(_call(),),
    )
    follow_up = ConversationMessage(
        role=MessageRole.TOOL,
        text="s3-bucket",
        tool_call_id="call-1",
    )
    client = FakeModelClient(
        (
            ModelResponse(tool_calls=(_call(),)),
            ModelResponse(text="done"),
        )
    )

    client.complete((user,), (spec,))
    client.complete((user, assistant, follow_up), (spec,))

    assert client.calls[0] == ModelRequest(messages=(user,), tools=(spec,))
    assert client.calls[1] == ModelRequest(
        messages=(user, assistant, follow_up),
        tools=(spec,),
    )
    assert client.calls[1].messages[-1].role is MessageRole.TOOL


def test_an_empty_script_raises_and_records_the_call() -> None:
    message = _user("hello")
    spec = _spec()
    client = FakeModelClient(())

    with pytest.raises(ModelClientError, match="no scripted response"):
        client.complete((message,), (spec,))

    assert client.calls == [ModelRequest(messages=(message,), tools=(spec,))]


def test_a_call_past_the_script_raises() -> None:
    client = FakeModelClient((ModelResponse(text="once"),))

    client.complete((), ())

    with pytest.raises(ModelClientError, match="no scripted response"):
        client.complete((), ())
    assert len(client.calls) == 2


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(
            name="list_catalog",
            description="List the catalog",
            parameters={},
            note="no",
        )
    with pytest.raises(ValidationError):
        ToolCall(
            call_id="call-1",
            name="list_catalog",
            arguments={},
            note="no",
        )
    with pytest.raises(ValidationError):
        ConversationMessage(role=MessageRole.USER, text="hello", note="no")
    with pytest.raises(ValidationError):
        ModelResponse(text="ready", note="no")


def test_an_empty_response_a_bare_tool_message_and_a_spaced_name_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelResponse()
    with pytest.raises(ValidationError):
        ConversationMessage(role=MessageRole.TOOL, text="s3-bucket")
    with pytest.raises(ValidationError):
        ToolCall(call_id="call-1", name="list catalog", arguments={})


def test_the_modules_do_not_import_network_libraries() -> None:
    found: list[str] = []
    for path in _MODULE_PATHS:
        relative = path.relative_to(_ROOT).as_posix()
        for name in _imported_names(path):
            if _forbidden(name):
                found.append(f"{relative}: {name}")

    assert found == []


def test_complete_does_not_open_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("socket opened")

    monkeypatch.setattr(socket, "socket", refuse)
    response = ModelResponse(text="ready")

    assert FakeModelClient((response,)).complete((), ()) == response

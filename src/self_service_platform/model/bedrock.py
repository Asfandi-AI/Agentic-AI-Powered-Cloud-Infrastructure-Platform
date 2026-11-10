"""Amazon Bedrock model client. The constructor does not call AWS."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from self_service_platform.core.model_client import (
    ConversationMessage,
    MessageRole,
    ModelClientError,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

_MAX_TOKENS = 4096
_NO_ANSWER = "the model returned no answer"
_REQUEST_FAILED = "the model request failed"
_BLOCKED = frozenset({"content_filtered", "guardrail_intervened"})


class BedrockRuntimeClient(Protocol):
    """The Converse call this client makes."""

    def converse(
        self,
        *,
        modelId: str,
        messages: Sequence[Mapping[str, Any]],
        system: Sequence[Mapping[str, Any]] | None = None,
        toolConfig: Mapping[str, Any] | None = None,
        inferenceConfig: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return one Converse response."""


class BedrockModelClient:
    """Calls Claude on Bedrock through the Converse API."""

    def __init__(
        self,
        client: BedrockRuntimeClient,
        *,
        model_id: str,
        region: str,
    ) -> None:
        self._client = client
        self.model_id = model_id
        self.region = region

    def complete(
        self,
        messages: Sequence[ConversationMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Return the next model turn for this conversation."""
        system, converse_messages = _converse_messages(messages)
        tool_config = _tool_config(tools)
        try:
            payload = _converse(
                self._client,
                model_id=self.model_id,
                messages=converse_messages,
                system=system,
                tool_config=tool_config,
            )
        except (ClientError, BotoCoreError) as exc:
            raise ModelClientError(_REQUEST_FAILED) from exc
        return _model_response(payload)


def _converse(
    client: BedrockRuntimeClient,
    *,
    model_id: str,
    messages: Sequence[Mapping[str, Any]],
    system: Sequence[Mapping[str, Any]],
    tool_config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    inference = {"maxTokens": _MAX_TOKENS}
    if system and tool_config is not None:
        return client.converse(
            modelId=model_id,
            messages=messages,
            system=system,
            toolConfig=tool_config,
            inferenceConfig=inference,
        )
    if system:
        return client.converse(
            modelId=model_id,
            messages=messages,
            system=system,
            inferenceConfig=inference,
        )
    if tool_config is not None:
        return client.converse(
            modelId=model_id,
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig=inference,
        )
    return client.converse(
        modelId=model_id,
        messages=messages,
        inferenceConfig=inference,
    )


def _tool_config(tools: Sequence[ToolSpec]) -> dict[str, Any] | None:
    if not tools:
        return None
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {"json": dict(tool.parameters)},
                }
            }
            for tool in tools
        ]
    }


def _converse_messages(
    messages: Sequence[ConversationMessage],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    system: list[dict[str, str]] = []
    converse: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.SYSTEM:
            _flush_tools(converse, tool_results)
            system.append({"text": _required_text(message)})
            continue
        if message.role is MessageRole.TOOL:
            tool_results.append(_tool_result(message))
            continue
        _flush_tools(converse, tool_results)
        if message.role is MessageRole.USER:
            converse.append(
                {"role": "user", "content": [{"text": _required_text(message)}]}
            )
            continue
        converse.append({"role": "assistant", "content": _assistant_content(message)})
    _flush_tools(converse, tool_results)
    return system, converse


def _flush_tools(
    converse: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> None:
    if not tool_results:
        return
    converse.append({"role": "user", "content": list(tool_results)})
    tool_results.clear()


def _tool_result(message: ConversationMessage) -> dict[str, Any]:
    call_id = message.tool_call_id
    if call_id is None:
        raise ModelClientError(_REQUEST_FAILED)
    return {
        "toolResult": {
            "toolUseId": call_id,
            "content": [{"text": _required_text(message)}],
        }
    }


def _assistant_content(message: ConversationMessage) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if message.text is not None:
        content.append({"text": message.text})
    for call in message.tool_calls:
        content.append(
            {
                "toolUse": {
                    "toolUseId": call.call_id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
            }
        )
    if not content:
        raise ModelClientError(_REQUEST_FAILED)
    return content


def _required_text(message: ConversationMessage) -> str:
    if message.text is None:
        raise ModelClientError(_REQUEST_FAILED)
    return message.text


def _model_response(payload: Mapping[str, Any]) -> ModelResponse:
    if payload.get("stopReason") in _BLOCKED:
        raise ModelClientError(_NO_ANSWER)
    content = _content(payload)
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise ModelClientError(_NO_ANSWER)
        if "toolUse" in block:
            calls.append(_tool_call(block["toolUse"]))
            continue
        if "text" in block:
            texts.append(_text_block(block["text"]))
    text = "".join(texts) if texts else None
    try:
        return ModelResponse(text=text, tool_calls=tuple(calls))
    except ValidationError as exc:
        raise ModelClientError(_NO_ANSWER) from exc


def _content(payload: Mapping[str, Any]) -> Sequence[Any]:
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise ModelClientError(_NO_ANSWER)
    message = output.get("message")
    if not isinstance(message, Mapping):
        raise ModelClientError(_NO_ANSWER)
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ModelClientError(_NO_ANSWER)
    return content


def _tool_call(value: Any) -> ToolCall:
    if not isinstance(value, Mapping):
        raise ModelClientError(_NO_ANSWER)
    call_id = value.get("toolUseId")
    name = value.get("name")
    arguments = value.get("input")
    if (
        not isinstance(call_id, str)
        or not isinstance(name, str)
        or not isinstance(arguments, Mapping)
    ):
        raise ModelClientError(_NO_ANSWER)
    try:
        return ToolCall(call_id=call_id, name=name, arguments=dict(arguments))
    except ValidationError as exc:
        raise ModelClientError(_NO_ANSWER) from exc


def _text_block(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelClientError(_NO_ANSWER)
    return value

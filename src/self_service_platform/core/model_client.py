"""The language-model port. No Bedrock client lives here."""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ModelClientError(Exception):
    """Raised when the model client cannot answer."""


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    parameters: Mapping[str, Any]

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _require_tool_name(value)

    @field_validator("parameters", mode="after")
    @classmethod
    def _freeze_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _read_only(value)


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: Mapping[str, Any]

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _require_tool_name(value)

    @field_validator("arguments", mode="after")
    @classmethod
    def _freeze_arguments(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _read_only(value)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MessageRole
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_role(self) -> Self:
        if self.role is MessageRole.TOOL:
            if self.text is None or self.tool_call_id is None:
                raise ValueError("a tool message has text and a tool call id")
            if len(self.tool_calls) > 0:
                raise ValueError("a tool message has no tool calls")
            return self
        if self.role is MessageRole.ASSISTANT:
            return self
        if self.text is None or len(self.tool_calls) > 0:
            raise ValueError("this role has text and no tool calls")
        return self


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def _has_content(self) -> Self:
        if self.text is None and len(self.tool_calls) == 0:
            raise ValueError("a response has text or a tool call")
        return self


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ConversationMessage, ...]
    tools: tuple[ToolSpec, ...]


class ModelClient(Protocol):
    def complete(
        self,
        messages: Sequence[ConversationMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Return the next model turn for this conversation."""


def _require_tool_name(value: str) -> str:
    if _TOOL_NAME.fullmatch(value) is None:
        raise ValueError("tool name is not allowed")
    return value


def _read_only(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))

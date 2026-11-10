"""A scripted model client for tests. It does not use the network."""

from collections.abc import Sequence

from self_service_platform.core.model_client import (
    ConversationMessage,
    ModelClientError,
    ModelRequest,
    ModelResponse,
    ToolSpec,
)


class FakeModelClient:
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._pending = list(responses)
        self.calls: list[ModelRequest] = []

    def complete(
        self,
        messages: Sequence[ConversationMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Record the request and return the next scripted response."""
        self.calls.append(ModelRequest(messages=tuple(messages), tools=tuple(tools)))
        if not self._pending:
            raise ModelClientError("no scripted response")
        return self._pending.pop(0)

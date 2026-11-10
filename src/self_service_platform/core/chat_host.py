"""The host a chat session uses to read, publish, and confirm."""

from typing import Protocol

from self_service_platform.core.service import (
    CreatePlan,
    DestroyPlan,
    UpdateConfirmations,
    UpdatePlan,
)


class ChatHost(Protocol):
    """Reads the user, publishes model text, and confirms plans."""

    def read_message(self) -> str | None:
        """Return the next user line, or None at end of input."""
        ...

    def publish(self, text: str) -> None:
        """Show one finished model reply."""
        ...

    def confirm_create(self, plan: CreatePlan) -> bool:
        """Return whether the user accepted this create plan."""
        ...

    def confirm_update(self, plan: UpdatePlan) -> UpdateConfirmations | None:
        """Return the user's update confirmation, or None when declined."""
        ...

    def confirm_destroy(self, plan: DestroyPlan) -> bool:
        """Return whether the user accepted this destroy plan."""
        ...

"""The tool port. A tool calls PlatformService and nothing else."""

from collections.abc import Mapping
from typing import Any, Protocol

from self_service_platform.core.service import PlatformService


class Tool(Protocol):
    name: str
    description: str
    parameters: Mapping[str, Any]

    def execute(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Run against the service and return a JSON-compatible result."""

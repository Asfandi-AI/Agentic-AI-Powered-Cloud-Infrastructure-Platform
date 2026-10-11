from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from self_service_platform.core.tenant import TenantType
from self_service_platform.core.validator import ValidatedManifest


class ExecutorError(Exception):
    """Raised when an executor operation fails."""


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    tenant_type: TenantType
    owner: str
    manifest: ValidatedManifest


class ResourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource_type: str
    identifier: str
    tags: Mapping[str, str]

    @field_validator("tags", mode="after")
    @classmethod
    def _freeze_tags(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))


class Executor(Protocol):
    def provision(self, request: ProvisionRequest) -> None:
        """Make the tenant's resources match the manifest."""

    def destroy(self, tenant_id: str) -> None:
        """Remove the tenant. An empty tenant succeeds."""

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        """Report tagged resources. component filters by the component tag."""

    def tenant_role_exists(self, tenant_id: str) -> bool:
        """Report whether the tenant role exists."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from self_service_platform.core.tenant import (
    TenantRecord,
    TenantStatus,
    TransitionFields,
)

Clock = Callable[[], datetime]


class Registry(Protocol):
    def create_tenant(self, record: TenantRecord) -> None:
        """Insert a tenant that is in status creating."""

    def get(self, tenant_id: str) -> TenantRecord:
        """Return the stored tenant."""

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        """Return every tenant, sorted by tenant_id."""

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        """Compare-and-set the status and store the fields when it succeeds."""

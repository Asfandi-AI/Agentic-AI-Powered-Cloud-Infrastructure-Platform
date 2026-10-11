from collections.abc import Mapping
from types import MappingProxyType

from self_service_platform.core.executor import ProvisionRequest

MANAGED_BY = "self-service-platform"


def build_tags(request: ProvisionRequest, entry_name: str) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "tenant_id": request.tenant_id,
            "tenant_type": request.tenant_type.value,
            "owner": request.owner,
            "managed_by": MANAGED_BY,
            "component": entry_name,
        }
    )

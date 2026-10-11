from dataclasses import dataclass, field

from self_service_platform.catalog.component import AwsService
from self_service_platform.core.executor import (
    ExecutorError,
    ProvisionRequest,
    ResourceRef,
)
from self_service_platform.core.tags import build_tags
from self_service_platform.core.validator import ValidatedEntry

_S3 = AwsService.S3.value


@dataclass
class _Resource:
    resource_type: str
    identifier: str
    tags: dict[str, str]
    object_keys: set[str] = field(default_factory=set)


@dataclass
class _TenantState:
    resources: dict[str, _Resource] = field(default_factory=dict)
    role_exists: bool = False


class FakeExecutor:
    def __init__(self) -> None:
        self._tenants: dict[str, _TenantState] = {}
        self._fail_after: int | None = None
        self._leave_one_on_destroy = False

    def fail_next_provision(self, *, after: int = 0) -> None:
        self._fail_after = after

    def leave_one_resource_on_next_destroy(self) -> None:
        self._leave_one_on_destroy = True

    def put_bucket_object(self, tenant_id: str, component: str, key: str) -> None:
        resource = self._require_resource(tenant_id, component)
        if resource.resource_type != _S3:
            raise ExecutorError(f"{component} is not an S3 bucket")
        resource.object_keys.add(key)

    def delete_component_resource(self, tenant_id: str, component: str) -> None:
        state = self._require_tenant(tenant_id)
        self._require_resource(tenant_id, component)
        self._remove_resource(state, component, empty_bucket=False)

    def provision(self, request: ProvisionRequest) -> None:
        fail_after = self._fail_after
        self._fail_after = None
        state = self._tenants.setdefault(request.tenant_id, _TenantState())
        for index, entry in enumerate(request.manifest.entries):
            if fail_after is not None and index >= fail_after:
                raise ExecutorError("injected provision failure")
            self._upsert(state, request, entry)
        if fail_after is not None:
            raise ExecutorError("injected provision failure")
        desired = {entry.name for entry in request.manifest.entries}
        for name in list(state.resources):
            if name not in desired:
                self._remove_resource(state, name, empty_bucket=True)
        state.role_exists = True

    def destroy(self, tenant_id: str) -> None:
        leave_one = self._leave_one_on_destroy
        self._leave_one_on_destroy = False
        state = self._tenants.get(tenant_id)
        if state is None:
            return
        names = sorted(state.resources)
        if leave_one and names:
            names = names[:-1]
        for name in names:
            self._remove_resource(state, name, empty_bucket=True)
        state.role_exists = False

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        state = self._tenants.get(tenant_id)
        if state is None:
            return ()
        refs = [
            _resource_ref(resource)
            for resource in state.resources.values()
            if component is None or resource.tags["component"] == component
        ]
        return tuple(sorted(refs, key=lambda ref: ref.identifier))

    def tenant_role_exists(self, tenant_id: str) -> bool:
        state = self._tenants.get(tenant_id)
        if state is None:
            return False
        return state.role_exists

    def _upsert(
        self,
        state: _TenantState,
        request: ProvisionRequest,
        entry: ValidatedEntry,
    ) -> None:
        resource_type = entry.component.aws_service.value
        existing = state.resources.get(entry.name)
        object_keys: set[str] = set()
        if (
            existing is not None
            and existing.resource_type == _S3
            and resource_type == _S3
        ):
            object_keys = set(existing.object_keys)
        state.resources[entry.name] = _Resource(
            resource_type=resource_type,
            identifier=f"{request.tenant_id}/{entry.name}",
            tags=dict(build_tags(request, entry.name)),
            object_keys=object_keys,
        )

    def _remove_resource(
        self,
        state: _TenantState,
        name: str,
        *,
        empty_bucket: bool,
    ) -> None:
        resource = state.resources[name]
        if resource.resource_type == _S3 and resource.object_keys:
            if not empty_bucket:
                raise ExecutorError(f"bucket {resource.identifier} is not empty")
            resource.object_keys.clear()
        del state.resources[name]

    def _require_tenant(self, tenant_id: str) -> _TenantState:
        try:
            return self._tenants[tenant_id]
        except KeyError:
            raise ExecutorError(f"no tenant {tenant_id}") from None

    def _require_resource(self, tenant_id: str, component: str) -> _Resource:
        state = self._require_tenant(tenant_id)
        try:
            return state.resources[component]
        except KeyError:
            raise ExecutorError(f"no resource {component} for {tenant_id}") from None


def _resource_ref(resource: _Resource) -> ResourceRef:
    return ResourceRef(
        resource_type=resource.resource_type,
        identifier=resource.identifier,
        tags=resource.tags,
    )

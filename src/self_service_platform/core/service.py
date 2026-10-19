from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.core.cost import EstimatedCost, estimate_cost
from self_service_platform.core.diff import ManifestDiff, compute_diff
from self_service_platform.core.executor import (
    Executor,
    ExecutorError,
    ProvisionRequest,
    ResourceRef,
)
from self_service_platform.core.manifest import Manifest, ManifestEntry
from self_service_platform.core.registry import Clock, Registry
from self_service_platform.core.tenant import (
    Initiator,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
    truncate_error,
)
from self_service_platform.core.validator import (
    ManifestValidationError,
    ManifestValidationIssue,
    ValidatedManifest,
    validate_manifest,
)


class ConfirmationRequiredError(Exception):
    """Raised when create is applied without the confirmation True."""


class StalePlanError(Exception):
    """Raised when a plan's catalog version or expiry is no longer current."""


class ProvisionFailedError(Exception):
    """Raised when provision fails with an ExecutorError."""


class TtlPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    dev: timedelta
    ephemeral: timedelta


class CreatePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    tenant_type: TenantType
    owner: str
    manifest: ValidatedManifest
    catalog_version: str
    estimated_cost: EstimatedCost
    expires_at: datetime | None


class CreateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: TenantRecord


class UpdateNotAllowedError(Exception):
    """Raised when the tenant status does not allow an update."""


class StoredManifestInvalidError(Exception):
    """Raised when the stored manifest fails the current catalog."""

    def __init__(self, issues: list[ManifestValidationIssue]) -> None:
        self.issues = issues
        detail = "; ".join(
            f"position {issue.position} {issue.name}: {issue.message}"
            for issue in issues
        )
        super().__init__(detail)


class UpdatePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    base_manifest: ValidatedManifest
    new_manifest: ValidatedManifest
    catalog_version: str
    diff: ManifestDiff
    estimated_cost: EstimatedCost
    nothing_to_change: bool
    removals: tuple[str, ...]
    replacements: tuple[str, ...]


class UpdateConfirmations(BaseModel):
    model_config = ConfigDict(frozen=True)

    update: bool
    removals: bool
    replacements: frozenset[str]


class UpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    changed: bool
    record: TenantRecord


class UpdateFailedError(Exception):
    """Raised when an update provision or removal check fails."""


class DestroyNotAllowedError(Exception):
    """Raised when the tenant status does not allow a destroy."""


class DestroyFailedError(Exception):
    """Raised when destroy or its verification fails."""


class DestroyPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    tenant_type: TenantType
    component_names: tuple[str, ...]
    status: TenantStatus
    statement: str


class DestroyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: TenantRecord


_DESTROY_STATEMENT = "all resources and data of the tenant will be deleted"


class PlatformService:
    def __init__(
        self,
        catalog: Catalog,
        registry: Registry,
        executor: Executor,
        clock: Clock,
        ttl_policy: TtlPolicy,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._executor = executor
        self._clock = clock
        self._ttl_policy = ttl_policy

    def plan_create(
        self,
        tenant_id: str,
        tenant_type: TenantType,
        owner: str,
        manifest: Manifest,
    ) -> CreatePlan:
        validated = validate_manifest(manifest, self._catalog)
        return CreatePlan(
            tenant_id=tenant_id,
            tenant_type=tenant_type,
            owner=owner,
            manifest=validated,
            catalog_version=validated.catalog_version,
            estimated_cost=estimate_cost(validated),
            expires_at=self._expires_at(tenant_type),
        )

    def plan_update(self, tenant_id: str, manifest: Manifest) -> UpdatePlan:
        record = self._registry.get(tenant_id)
        _require_updatable(record.status)
        base = self._validated_stored(record.manifest)
        proposed = validate_manifest(manifest, self._catalog)
        diff = compute_diff(applied=base, proposed=proposed)
        return UpdatePlan(
            tenant_id=tenant_id,
            base_manifest=base,
            new_manifest=proposed,
            catalog_version=self._catalog.catalog_version,
            diff=diff,
            estimated_cost=estimate_cost(proposed),
            nothing_to_change=diff.is_empty,
            removals=tuple(entry.name for entry in diff.removed),
            replacements=tuple(entry.name for entry in diff.replacements),
        )

    def apply_update(
        self,
        plan: UpdatePlan,
        confirmations: UpdateConfirmations,
        initiator: Initiator,
    ) -> UpdateResult:
        if plan.nothing_to_change:
            current = self._registry.get(plan.tenant_id)
            return UpdateResult(changed=False, record=current)
        _refuse_unconfirmed(plan, confirmations)
        record = self._ready_to_update(plan)
        self._mark_updating(record, initiator)
        try:
            self._executor.provision(_update_request(record, plan))
        except ExecutorError as exc:
            self._mark_update_failed(record, initiator, exc)
        leftovers = self._leftover_names(plan)
        if leftovers:
            cause = Exception(f"leftover resources remain: {', '.join(leftovers)}")
            self._mark_update_failed(record, initiator, cause)
        updated = self._mark_active(record, plan, initiator)
        return UpdateResult(changed=True, record=updated)

    def apply_create(
        self,
        plan: CreatePlan,
        confirmed: object,
        initiator: Initiator,
    ) -> CreateResult:
        self._refuse_unless_current(plan, confirmed)
        manifest = _manifest_entries(plan.manifest)
        self._registry.create_tenant(self._creating_record(plan, initiator, manifest))
        self._provision(plan)
        record = self._activate(plan, initiator, manifest)
        return CreateResult(record=record)

    def plan_destroy(self, tenant_id: str) -> DestroyPlan:
        record = self._registry.get(tenant_id)
        _require_destroyable(record.status)
        return DestroyPlan(
            tenant_id=record.tenant_id,
            tenant_type=record.tenant_type,
            component_names=tuple(entry.name for entry in record.manifest),
            status=record.status,
            statement=_DESTROY_STATEMENT,
        )

    def apply_destroy(
        self,
        plan: DestroyPlan,
        confirmed: object,
        initiator: Initiator,
    ) -> DestroyResult:
        if confirmed is not True:
            raise ConfirmationRequiredError("confirmation must be True")
        record = self._ready_to_destroy(plan)
        self._mark_destroying(record, initiator)
        self._run_destroy(record, initiator)
        return DestroyResult(record=self._mark_destroyed(record, initiator))

    def _expires_at(self, tenant_type: TenantType) -> datetime | None:
        ttl = _ttl_for(tenant_type, self._ttl_policy)
        if ttl is None:
            return None
        return self._clock() + ttl

    def _refuse_unless_current(self, plan: CreatePlan, confirmed: object) -> None:
        if confirmed is not True:
            raise ConfirmationRequiredError("confirmation must be True")
        if plan.catalog_version != self._catalog.catalog_version:
            raise StalePlanError("catalog version does not match")
        if plan.tenant_type is TenantType.PROD:
            return
        expires_at = plan.expires_at
        if expires_at is None or expires_at <= self._clock():
            raise StalePlanError("plan has expired")

    def _validated_stored(
        self,
        entries: tuple[ManifestEntry, ...],
    ) -> ValidatedManifest:
        stored = Manifest.model_validate([entry.model_dump() for entry in entries])
        try:
            return validate_manifest(stored, self._catalog)
        except ManifestValidationError as exc:
            raise StoredManifestInvalidError(exc.issues) from exc

    def _ready_to_update(self, plan: UpdatePlan) -> TenantRecord:
        record = self._registry.get(plan.tenant_id)
        if plan.catalog_version != self._catalog.catalog_version:
            raise StalePlanError("catalog version does not match")
        if not self._matches_base(record.manifest, plan.base_manifest):
            raise StalePlanError("stored manifest does not match the plan")
        _require_updatable(record.status)
        return record

    def _matches_base(
        self,
        entries: tuple[ManifestEntry, ...],
        base: ValidatedManifest,
    ) -> bool:
        stored = Manifest.model_validate([entry.model_dump() for entry in entries])
        try:
            current = validate_manifest(stored, self._catalog)
        except ManifestValidationError as exc:
            raise StalePlanError(
                "stored manifest no longer validates against the current catalog"
            ) from exc
        return current == base

    def _mark_updating(self, record: TenantRecord, initiator: Initiator) -> None:
        self._registry.transition(
            record.tenant_id,
            record.status,
            TenantStatus.UPDATING,
            _carried_fields(record, initiator, record.last_error),
        )

    def _leftover_names(self, plan: UpdatePlan) -> tuple[str, ...]:
        added = {entry.name for entry in plan.diff.added}
        names: list[str] = []
        for name in plan.removals:
            found = self._executor.list_tagged_resources(plan.tenant_id, component=name)
            if name in added:
                new_type = _component_service(plan.new_manifest, name)
                found = tuple(
                    resource for resource in found if resource.resource_type != new_type
                )
            if found:
                names.append(name)
        return tuple(names)

    def _mark_update_failed(
        self,
        record: TenantRecord,
        initiator: Initiator,
        exc: BaseException,
    ) -> None:
        message = truncate_error(str(exc))
        self._registry.transition(
            record.tenant_id,
            TenantStatus.UPDATING,
            TenantStatus.UPDATE_FAILED,
            _carried_fields(record, initiator, message),
        )
        raise UpdateFailedError(message) from exc

    def _mark_active(
        self,
        record: TenantRecord,
        plan: UpdatePlan,
        initiator: Initiator,
    ) -> TenantRecord:
        return self._registry.transition(
            record.tenant_id,
            TenantStatus.UPDATING,
            TenantStatus.ACTIVE,
            TransitionFields(
                manifest=_manifest_entries(plan.new_manifest),
                catalog_version=plan.catalog_version,
                initiator=initiator,
                expires_at=record.expires_at,
                last_error=None,
            ),
        )

    def _creating_record(
        self,
        plan: CreatePlan,
        initiator: Initiator,
        manifest: tuple[ManifestEntry, ...],
    ) -> TenantRecord:
        return TenantRecord(
            tenant_id=plan.tenant_id,
            tenant_type=plan.tenant_type,
            owner=plan.owner,
            status=TenantStatus.CREATING,
            manifest=manifest,
            catalog_version=plan.catalog_version,
            initiator=initiator,
            expires_at=plan.expires_at,
            last_error=None,
            updated_at=self._clock(),
        )

    def _provision(self, plan: CreatePlan) -> None:
        request = ProvisionRequest(
            tenant_id=plan.tenant_id,
            tenant_type=plan.tenant_type,
            owner=plan.owner,
            manifest=plan.manifest,
        )
        try:
            self._executor.provision(request)
        except ExecutorError as exc:
            raise ProvisionFailedError(truncate_error(str(exc))) from exc

    def _activate(
        self,
        plan: CreatePlan,
        initiator: Initiator,
        manifest: tuple[ManifestEntry, ...],
    ) -> TenantRecord:
        return self._registry.transition(
            plan.tenant_id,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            TransitionFields(
                manifest=manifest,
                catalog_version=plan.catalog_version,
                initiator=initiator,
                expires_at=plan.expires_at,
                last_error=None,
            ),
        )

    def _ready_to_destroy(self, plan: DestroyPlan) -> TenantRecord:
        record = self._registry.get(plan.tenant_id)
        if record.status is not plan.status:
            raise StalePlanError("stored status does not match the plan")
        _require_destroyable(record.status)
        return record

    def _mark_destroying(self, record: TenantRecord, initiator: Initiator) -> None:
        self._registry.transition(
            record.tenant_id,
            record.status,
            TenantStatus.DESTROYING,
            _carried_fields(record, initiator, record.last_error),
        )

    def _run_destroy(self, record: TenantRecord, initiator: Initiator) -> None:
        try:
            self._executor.destroy(record.tenant_id)
        except ExecutorError as exc:
            raise self._destroy_failed(record, initiator, exc) from exc
        try:
            leftovers = self._executor.list_tagged_resources(record.tenant_id)
        except ExecutorError as exc:
            raise self._destroy_failed(record, initiator, exc) from exc
        try:
            role_exists = self._executor.tenant_role_exists(record.tenant_id)
        except ExecutorError as exc:
            raise self._destroy_failed(record, initiator, exc) from exc
        if leftovers or role_exists:
            cause = _verification_failure(leftovers, role_exists)
            raise self._destroy_failed(record, initiator, cause) from cause

    def _mark_destroyed(
        self,
        record: TenantRecord,
        initiator: Initiator,
    ) -> TenantRecord:
        return self._registry.transition(
            record.tenant_id,
            TenantStatus.DESTROYING,
            TenantStatus.DESTROYED,
            _carried_fields(record, initiator, None),
        )

    def _destroy_failed(
        self,
        record: TenantRecord,
        initiator: Initiator,
        exc: BaseException,
    ) -> DestroyFailedError:
        message = truncate_error(str(exc))
        self._registry.transition(
            record.tenant_id,
            TenantStatus.DESTROYING,
            TenantStatus.DESTROY_FAILED,
            _carried_fields(record, initiator, message),
        )
        return DestroyFailedError(message)


def _refuse_unconfirmed(plan: UpdatePlan, confirmations: UpdateConfirmations) -> None:
    missing: list[str] = []
    if confirmations.update is not True:
        missing.append("update")
    if plan.removals and confirmations.removals is not True:
        missing.append("removals")
    missing.extend(
        name for name in plan.replacements if name not in confirmations.replacements
    )
    if missing:
        detail = ", ".join(missing)
        raise ConfirmationRequiredError(f"missing confirmation: {detail}")


def _update_request(record: TenantRecord, plan: UpdatePlan) -> ProvisionRequest:
    return ProvisionRequest(
        tenant_id=plan.tenant_id,
        tenant_type=record.tenant_type,
        owner=record.owner,
        manifest=plan.new_manifest,
    )


def _carried_fields(
    record: TenantRecord,
    initiator: Initiator,
    last_error: str | None,
) -> TransitionFields:
    return TransitionFields(
        manifest=record.manifest,
        catalog_version=record.catalog_version,
        initiator=initiator,
        expires_at=record.expires_at,
        last_error=last_error,
    )


def _component_service(manifest: ValidatedManifest, name: str) -> str:
    for entry in manifest.entries:
        if entry.name == name:
            return entry.component.aws_service.value
    raise ValueError(f"no component {name}")


def _require_updatable(status: TenantStatus) -> None:
    if status is TenantStatus.ACTIVE or status is TenantStatus.UPDATE_FAILED:
        return
    raise UpdateNotAllowedError(f"cannot update status {status.value}")


def _require_destroyable(status: TenantStatus) -> None:
    if status in (
        TenantStatus.ACTIVE,
        TenantStatus.UPDATE_FAILED,
        TenantStatus.DESTROY_FAILED,
    ):
        return
    raise DestroyNotAllowedError(f"cannot destroy status {status.value}")


def _verification_failure(
    leftovers: tuple[ResourceRef, ...],
    role_exists: bool,
) -> Exception:
    parts: list[str] = []
    if leftovers:
        identifiers = ", ".join(resource.identifier for resource in leftovers)
        parts.append(f"leftover resources remain: {identifiers}")
    if role_exists:
        parts.append("tenant role remains")
    return Exception("; ".join(parts))


def _ttl_for(tenant_type: TenantType, policy: TtlPolicy) -> timedelta | None:
    if tenant_type is TenantType.PROD:
        return None
    if tenant_type is TenantType.DEV:
        return policy.dev
    return policy.ephemeral


def _manifest_entries(manifest: ValidatedManifest) -> tuple[ManifestEntry, ...]:
    return tuple(
        ManifestEntry(
            type=entry.component.component_type,
            name=entry.name,
            options=entry.options.model_dump(),
        )
        for entry in manifest.entries
    )

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.core.cost import EstimatedCost, estimate_cost
from self_service_platform.core.diff import ManifestDiff, compute_diff
from self_service_platform.core.executor import (
    Executor,
    ExecutorError,
    ProvisionRequest,
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


def _require_updatable(status: TenantStatus) -> None:
    if status is TenantStatus.ACTIVE or status is TenantStatus.UPDATE_FAILED:
        return
    raise UpdateNotAllowedError(f"cannot update status {status.value}")


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

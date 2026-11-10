"""Twelve tools. Confirmation is issued by the host, not by the model."""

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from self_service_platform.agent.tool import Tool
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    CreatePlan,
    DestroyPlan,
    PlatformService,
    UpdateConfirmations,
    UpdatePlan,
)
from self_service_platform.core.tenant import Initiator, TenantType
from self_service_platform.core.validator import (
    ManifestValidationError,
    ValidatedManifest,
)

_HOST_ONLY = frozenset({"data_loss_confirmed", "removals", "replacements"})
_Run = Callable[[PlatformService, Mapping[str, Any]], Mapping[str, Any]]
_PlanT = TypeVar("_PlanT", CreatePlan, UpdatePlan, DestroyPlan)


class _Tool:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
        run: _Run,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self._run = run

    def execute(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._run(service, arguments)


def _schema(
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {} if properties is None else properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _entries_schema() -> dict[str, Any]:
    return _schema(
        {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "name": {"type": "string"},
                        "options": {"type": "object"},
                    },
                    "required": ["type", "name"],
                    "additionalProperties": False,
                },
            }
        },
        ("entries",),
    )


def _tenant_schema(*fields: str) -> dict[str, Any]:
    properties: dict[str, Any] = {"tenant_id": {"type": "string"}}
    required = ["tenant_id"]
    for field in fields:
        properties[field] = {"type": "string"}
        required.append(field)
    return _schema(properties, tuple(required))


def _plan_create_schema() -> dict[str, Any]:
    schema = _entries_schema()
    properties = schema["properties"]
    properties["tenant_id"] = {"type": "string"}
    properties["tenant_type"] = {"type": "string"}
    properties["owner"] = {"type": "string"}
    schema["required"] = ["tenant_id", "tenant_type", "owner", "entries"]
    return schema


def _plan_update_schema() -> dict[str, Any]:
    schema = _entries_schema()
    schema["properties"]["tenant_id"] = {"type": "string"}
    schema["required"] = ["tenant_id", "entries"]
    return schema


def _manifest(arguments: Mapping[str, Any]) -> Manifest:
    return Manifest.model_validate(list(arguments["entries"]))


def _token(arguments: Mapping[str, Any]) -> str:
    token = arguments.get("token", "")
    if not isinstance(token, str):
        return ""
    return token


def _issues(error: ManifestValidationError) -> dict[str, Any]:
    return {
        "issues": [
            {"position": issue.position, "name": issue.name, "message": issue.message}
            for issue in error.issues
        ]
    }


def _amount(total: object) -> str | None:
    if total is None:
        return None
    return str(total)


def _cost(total: object, label: str) -> dict[str, Any]:
    text = "not priced" if total is None else str(total)
    return {"total": _amount(total), "label": label, "summary": text}


class AgentTools:
    def __init__(self, initiator: Initiator) -> None:
        self._initiator = initiator
        self._plans: dict[str, CreatePlan | UpdatePlan | DestroyPlan] = {}
        self._updates: dict[str, UpdateConfirmations] = {}
        self._tools: tuple[Tool, ...] = (
            _Tool(
                "list_catalog",
                "List the catalog components.",
                _schema(),
                self._list_catalog,
            ),
            _Tool(
                "draft_manifest",
                "Validate a draft manifest against the catalog.",
                _entries_schema(),
                self._draft_manifest,
            ),
            _Tool(
                "validate_manifest",
                "Validate a manifest with the same checks as the CLI.",
                _entries_schema(),
                self._validate_manifest,
            ),
            _Tool(
                "estimate",
                "Estimate the cost of a manifest. Unpriced items stay unpriced.",
                _entries_schema(),
                self._estimate,
            ),
            _Tool(
                "plan_create",
                "Plan a new tenant. This does not create it or return a token.",
                _plan_create_schema(),
                self._plan_create,
            ),
            _Tool(
                "plan_update",
                "Plan an update. This does not change the tenant or return a token.",
                _plan_update_schema(),
                self._plan_update,
            ),
            _Tool(
                "execute_create",
                "Create the tenant from the remembered plan and a token.",
                _tenant_schema("token"),
                self._execute_create,
            ),
            _Tool(
                "execute_update",
                "Apply the remembered update with a token.",
                _tenant_schema("token"),
                self._execute_update,
            ),
            _Tool(
                "list_tenants", "List stored tenants.", _schema(), self._list_tenants
            ),
            _Tool(
                "tenant_status",
                "Return the stored status of one tenant.",
                _tenant_schema(),
                self._tenant_status,
            ),
            _Tool(
                "plan_destroy",
                "Plan deletion of a tenant. This does not destroy it or return a token.",
                _tenant_schema(),
                self._plan_destroy,
            ),
            _Tool(
                "execute_destroy",
                "Destroy the tenant from the remembered plan and a token.",
                _tenant_schema("token"),
                self._execute_destroy,
            ),
        )

    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def remembered_plan(self, tenant_id: str) -> CreatePlan | UpdatePlan | DestroyPlan:
        """Return the plan the host confirms. This is not a tool."""
        try:
            return self._plans[tenant_id]
        except KeyError:
            raise ConfirmationRequiredError("no plan") from None

    def accept_update(self, tenant_id: str, confirmations: UpdateConfirmations) -> None:
        """Store the host's update confirmation. This is not a tool."""
        self._updates[tenant_id] = confirmations

    def _list_catalog(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del arguments
        components, version = service.list_catalog(self._initiator)
        return {
            "catalog_version": version,
            "components": [
                {
                    "component_type": component.component_type,
                    "description": component.description,
                    "service": component.aws_service.value,
                }
                for component in components
            ],
        }

    def _draft_manifest(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._checked(service.draft_manifest, arguments)

    def _validate_manifest(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._checked(service.validate_manifest, arguments)

    def _estimate(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        cost = service.estimate(_manifest(arguments), self._initiator)
        return _cost(cost.total, cost.label)

    def _plan_create(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        plan = service.plan_create(
            str(arguments["tenant_id"]),
            TenantType(str(arguments["tenant_type"])),
            str(arguments["owner"]),
            _manifest(arguments),
            self._initiator,
        )
        self._plans[plan.tenant_id] = plan
        expires_at = plan.expires_at
        return {
            "catalog_version": plan.catalog_version,
            "expires_at": None if expires_at is None else expires_at.isoformat(),
            "entry_count": len(plan.manifest.entries),
            "estimated_cost": _cost(
                plan.estimated_cost.total, plan.estimated_cost.label
            ),
        }

    def _plan_update(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        tenant_id = str(arguments["tenant_id"])
        plan = service.plan_update(tenant_id, _manifest(arguments), self._initiator)
        self._plans[tenant_id] = plan
        return {
            "tenant_id": plan.tenant_id,
            "removals": list(plan.removals),
            "replacements": list(plan.replacements),
            "nothing_to_change": plan.nothing_to_change,
            "estimated_cost": _cost(
                plan.estimated_cost.total, plan.estimated_cost.label
            ),
        }

    def _execute_create(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        plan = self._require(str(arguments["tenant_id"]), CreatePlan)
        result = service.apply_create(
            plan, True, self._initiator, token=_token(arguments)
        )
        return {
            "tenant_id": result.record.tenant_id,
            "status": result.record.status.value,
        }

    def _execute_update(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if any(name in arguments for name in _HOST_ONLY):
            raise ConfirmationRequiredError("the host confirms data loss")
        tenant_id = str(arguments["tenant_id"])
        confirmations = self._updates.get(tenant_id)
        if confirmations is None:
            raise ConfirmationRequiredError("the host has not accepted this update")
        plan = self._require(tenant_id, UpdatePlan)
        result = service.apply_update(
            plan, confirmations, self._initiator, token=_token(arguments)
        )
        return {"status": result.record.status.value}

    def _list_tenants(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del arguments
        records = service.list_tenants(self._initiator)
        return {
            "tenants": [
                {
                    "tenant_id": record.tenant_id,
                    "status": record.status.value,
                    "tenant_type": record.tenant_type.value,
                }
                for record in records
            ]
        }

    def _tenant_status(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        record = service.tenant_status(str(arguments["tenant_id"]), self._initiator)
        return {
            "tenant_id": record.tenant_id,
            "status": record.status.value,
            "tenant_type": record.tenant_type.value,
            "owner": record.owner,
            "catalog_version": record.catalog_version,
            "component_names": [entry.name for entry in record.manifest],
        }

    def _plan_destroy(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        tenant_id = str(arguments["tenant_id"])
        plan = service.plan_destroy(tenant_id, self._initiator)
        self._plans[tenant_id] = plan
        return {
            "component_names": list(plan.component_names),
            "statement": plan.statement,
            "status": plan.status.value,
            "tenant_type": plan.tenant_type.value,
        }

    def _execute_destroy(
        self, service: PlatformService, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        plan = self._require(str(arguments["tenant_id"]), DestroyPlan)
        result = service.apply_destroy(
            plan, True, self._initiator, token=_token(arguments)
        )
        return {"status": result.record.status.value}

    def _checked(
        self,
        validate: Callable[..., ValidatedManifest],
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            validated = validate(_manifest(arguments), self._initiator)
        except ManifestValidationError as error:
            return _issues(error)
        return {
            "catalog_version": validated.catalog_version,
            "entries": [
                {
                    "type": entry.component.component_type,
                    "name": entry.name,
                }
                for entry in validated.entries
            ],
        }

    def _require(self, tenant_id: str, kind: type[_PlanT]) -> _PlanT:
        plan = self.remembered_plan(tenant_id)
        if not isinstance(plan, kind):
            raise ConfirmationRequiredError("no plan")
        return plan

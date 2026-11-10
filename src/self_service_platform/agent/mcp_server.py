"""Stdio MCP session. The client confirms plans; the service checks the value."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from self_service_platform.agent.tools import AgentTools
from self_service_platform.core.audit import AuditLogError
from self_service_platform.core.confirmation import (
    DataLossNotConfirmedError,
    NothingToConfirmError,
    PlanMismatchError,
    ReplacementConfirmationError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    UnknownTokenError,
)
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    CreatePlan,
    DestroyFailedError,
    DestroyNotAllowedError,
    DestroyPlan,
    PlatformService,
    ProvisionFailedError,
    StalePlanError,
    StoredManifestInvalidError,
    UpdateConfirmations,
    UpdateFailedError,
    UpdateNotAllowedError,
    UpdatePlan,
)
from self_service_platform.core.tenant import (
    DuplicateTenantError,
    Initiator,
    TenantNotFoundError,
    TransitionRejectedError,
)
from self_service_platform.core.validator import ManifestValidationError

_PROTOCOL_VERSION = "2025-06-18"
_PLAN_TOOLS = frozenset({"plan_create", "plan_update", "plan_destroy"})
_Elicit = Callable[[dict[str, Any]], Mapping[str, Any]]
_TOOL_ERRORS = (
    ConfirmationRequiredError,
    DataLossNotConfirmedError,
    DestroyFailedError,
    DestroyNotAllowedError,
    DuplicateTenantError,
    KeyError,
    ManifestValidationError,
    NothingToConfirmError,
    PlanMismatchError,
    ProvisionFailedError,
    ReplacementConfirmationError,
    StalePlanError,
    StoredManifestInvalidError,
    TenantNotFoundError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TransitionRejectedError,
    TypeError,
    UnknownTokenError,
    UpdateFailedError,
    UpdateNotAllowedError,
    ValidationError,
    ValueError,
)


class McpSession:
    """One MCP session. Plans stay in memory until the process exits."""

    def __init__(
        self,
        service: PlatformService,
        initiator: Initiator,
        *,
        elicit: _Elicit,
    ) -> None:
        self._service = service
        self._initiator = initiator
        self._elicit = elicit
        self._agent = AgentTools(initiator)
        self._tools = {tool.name: tool for tool in self._agent.tools()}
        self._elicitation = False
        self._elicit_seq = 0

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Notifications return None."""
        if "id" not in message:
            return None
        try:
            return self._dispatch(message)
        except AuditLogError:
            return _failure(message["id"], -32603, "cannot write the audit log")

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        request_id = message["id"]
        if method == "initialize":
            return _ok(request_id, self._initialize(message.get("params")))
        if method == "tools/list":
            return _ok(request_id, {"tools": self._listed_tools()})
        if method == "tools/call":
            return _ok(request_id, self._call(message.get("params")))
        if method == "ping":
            return _ok(request_id, {})
        return _failure(request_id, -32601, "method not found")

    def _initialize(self, params: object) -> dict[str, Any]:
        capabilities: object = {}
        if isinstance(params, dict):
            capabilities = params.get("capabilities", {})
        self._elicitation = (
            isinstance(capabilities, dict) and "elicitation" in capabilities
        )
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ssp", "version": "0.1.0"},
        }

    def _listed_tools(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = []
        for tool in self._agent.tools():
            listed.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": dict(tool.parameters),
                }
            )
        return listed

    def _call(self, params: object) -> dict[str, Any]:
        name = ""
        arguments: dict[str, Any] = {}
        if isinstance(params, dict):
            raw_name = params.get("name", "")
            if isinstance(raw_name, str):
                name = raw_name
            raw_arguments = params.get("arguments", {})
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
        tool = self._tools.get(name)
        if tool is None:
            return _tool_result(json.dumps({"error": "unknown tool"}), error=True)
        try:
            payload = dict(tool.execute(self._service, arguments))
            if name in _PLAN_TOOLS:
                payload = self._confirm(name, arguments, payload)
        except AuditLogError:
            raise
        except _TOOL_ERRORS as exc:
            return _tool_result(json.dumps({"error": str(exc)}), error=True)
        return _tool_result(json.dumps(payload), error=False)

    def _confirm(
        self,
        name: str,
        arguments: Mapping[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tenant_id = arguments["tenant_id"]
        if not isinstance(tenant_id, str):
            raise ConfirmationRequiredError("no plan")
        plan = self._agent.remembered_plan(tenant_id)
        if name == "plan_create" and isinstance(plan, CreatePlan):
            return self._confirm_create(plan, payload)
        if name == "plan_update" and isinstance(plan, UpdatePlan):
            return self._confirm_update(plan, payload)
        if name == "plan_destroy" and isinstance(plan, DestroyPlan):
            return self._confirm_destroy(plan, payload)
        raise ConfirmationRequiredError("no plan")

    def _confirm_create(
        self, plan: CreatePlan, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._elicitation:
            self._service.decline_create(plan, self._initiator)
            return _declined(payload)
        content = self._ask(
            _create_message(plan), _accept_schema("Create this tenant?")
        )
        if content is None or content.get("accept") is not True:
            self._service.decline_create(plan, self._initiator)
            return _declined(payload)
        secret = self._service.confirm_create(plan, self._initiator)
        return _accepted(payload, secret)

    def _confirm_update(
        self, plan: UpdatePlan, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if plan.nothing_to_change:
            return _declined(payload)
        if not self._elicitation:
            self._service.decline_update(plan, self._initiator)
            return _declined(payload)
        content = self._ask(_update_message(plan), _update_schema(plan))
        chosen = _update_confirmations(plan, content)
        if chosen is None:
            self._service.decline_update(plan, self._initiator)
            return _declined(payload)
        self._agent.accept_update(plan.tenant_id, chosen)
        secret = self._service.confirm_update(
            plan,
            self._initiator,
            data_loss_confirmed=bool(plan.removals),
            confirmed_replacements=frozenset(plan.replacements),
        )
        return _accepted(payload, secret)

    def _confirm_destroy(
        self, plan: DestroyPlan, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._elicitation:
            self._service.decline_destroy(plan, self._initiator)
            return _declined(payload)
        content = self._ask(_destroy_message(plan), _typed_id_schema())
        typed = "" if content is None else content.get("tenant_id")
        if typed != plan.tenant_id:
            self._service.decline_destroy(plan, self._initiator)
            return _declined(payload)
        secret = self._service.confirm_destroy(plan, self._initiator)
        return _accepted(payload, secret)

    def _ask(self, message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        self._elicit_seq += 1
        answer = self._elicit(
            {
                "jsonrpc": "2.0",
                "id": f"elicit-{self._elicit_seq}",
                "method": "elicitation/create",
                "params": {"message": message, "requestedSchema": schema},
            }
        )
        if answer.get("action") != "accept":
            return None
        content = answer.get("content")
        if not isinstance(content, dict):
            return {}
        return content


def _create_message(plan: CreatePlan) -> str:
    cost = _priced(plan.estimated_cost.total)
    expires = "none" if plan.expires_at is None else plan.expires_at.isoformat()
    return (
        f"tenant_id: {plan.tenant_id}\n"
        f"tenant_type: {plan.tenant_type.value}\n"
        f"owner: {plan.owner}\n"
        f"{cost}\n"
        f"expires_at: {expires}"
    )


def _update_message(plan: UpdatePlan) -> str:
    removals = ", ".join(plan.removals) if plan.removals else "-"
    replacements = ", ".join(plan.replacements) if plan.replacements else "-"
    return (
        f"tenant_id: {plan.tenant_id}\n"
        f"removals: {removals}\n"
        f"replacements: {replacements}\n"
        f"{_priced(plan.estimated_cost.total)}"
    )


def _destroy_message(plan: DestroyPlan) -> str:
    names = ", ".join(plan.component_names) if plan.component_names else "-"
    return f"tenant_id: {plan.tenant_id}\ncomponents: {names}\n{plan.statement}"


def _priced(total: object) -> str:
    if total is None:
        return "not priced"
    return str(total)


def _accept_schema(title: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"accept": {"type": "boolean", "title": title}},
        "required": ["accept"],
    }


def _update_schema(plan: UpdatePlan) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "accept": {"type": "boolean", "title": "Apply this update?"}
    }
    required = ["accept"]
    if plan.removals:
        properties["removals"] = {
            "type": "boolean",
            "title": "Delete the data of these components?",
        }
        required.append("removals")
    if plan.replacements:
        properties["replacements"] = {
            "type": "array",
            "items": {"type": "string"},
            "title": "Recreate these components?",
        }
        required.append("replacements")
    return {"type": "object", "properties": properties, "required": required}


def _typed_id_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tenant_id": {
                "type": "string",
                "title": "Type the tenant id to confirm destroy",
            }
        },
        "required": ["tenant_id"],
    }


def _update_confirmations(
    plan: UpdatePlan, content: dict[str, Any] | None
) -> UpdateConfirmations | None:
    if content is None or content.get("accept") is not True:
        return None
    if plan.removals and content.get("removals") is not True:
        return None
    names = _replacement_names(content.get("replacements", []))
    if names is None or names != frozenset(plan.replacements):
        return None
    return UpdateConfirmations(
        update=True,
        removals=bool(plan.removals),
        replacements=frozenset(plan.replacements),
    )


def _replacement_names(value: object) -> frozenset[str] | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        names.append(item)
    return frozenset(names)


def _declined(payload: dict[str, Any]) -> dict[str, Any]:
    declined = dict(payload)
    declined.pop("token", None)
    declined["confirmed"] = False
    return declined


def _accepted(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    accepted = dict(payload)
    accepted["confirmed"] = True
    accepted["token"] = secret
    return accepted


def _tool_result(text: str, *, error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _ok(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _failure(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }

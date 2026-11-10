"""The chat loop. It calls the model and asks the host to confirm plans."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from self_service_platform.agent.tool import Tool
from self_service_platform.agent.tools import AgentTools
from self_service_platform.core.audit import AuditLogError
from self_service_platform.core.chat_host import ChatHost
from self_service_platform.core.confirmation import (
    DataLossNotConfirmedError,
    NothingToConfirmError,
    PlanMismatchError,
    ReplacementConfirmationError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    UnknownTokenError,
)
from self_service_platform.core.model_client import (
    ConversationMessage,
    MessageRole,
    ModelClient,
    ToolCall,
    ToolSpec,
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

_COMPLETION_LIMIT = 8
_PLAN_TOOLS = frozenset({"plan_create", "plan_update", "plan_destroy"})
_SYSTEM = (
    "You are the self-service platform assistant. "
    "Use only catalog components. "
    "Call plan_create, plan_update, or plan_destroy before the matching execute tool. "
    "After the user agrees, the host puts the confirmation value in that plan result. "
    "Pass that value to execute_create, execute_update, or execute_destroy."
)
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


def run_chat(
    service: PlatformService,
    client: ModelClient,
    initiator: Initiator,
    host: ChatHost,
) -> None:
    """Run one chat session until the user ends input."""
    agent = AgentTools(initiator)
    specs = tuple(
        ToolSpec(
            name=tool.name,
            description=tool.description,
            parameters=dict(tool.parameters),
        )
        for tool in agent.tools()
    )
    tools_by_name = {tool.name: tool for tool in agent.tools()}
    messages = [ConversationMessage(role=MessageRole.SYSTEM, text=_SYSTEM)]
    while True:
        line = host.read_message()
        if line is None:
            return
        messages.append(ConversationMessage(role=MessageRole.USER, text=line))
        _complete_turn(
            messages, client, specs, host, service, agent, initiator, tools_by_name
        )


def _complete_turn(
    messages: list[ConversationMessage],
    client: ModelClient,
    specs: tuple[ToolSpec, ...],
    host: ChatHost,
    service: PlatformService,
    agent: AgentTools,
    initiator: Initiator,
    tools_by_name: dict[str, Tool],
) -> None:
    for _step in range(_COMPLETION_LIMIT):
        response = client.complete(messages, specs)
        if len(response.tool_calls) == 0:
            finished = response.text if response.text is not None else ""
            host.publish(finished)
            messages.append(
                ConversationMessage(role=MessageRole.ASSISTANT, text=finished)
            )
            return
        messages.append(
            ConversationMessage(
                role=MessageRole.ASSISTANT,
                text=response.text,
                tool_calls=response.tool_calls,
            )
        )
        for call in response.tool_calls:
            messages.append(
                ConversationMessage(
                    role=MessageRole.TOOL,
                    text=_tool_text(
                        call, service, agent, initiator, host, tools_by_name
                    ),
                    tool_call_id=call.call_id,
                )
            )
    host.publish("the model did not finish")


def _tool_text(
    call: ToolCall,
    service: PlatformService,
    agent: AgentTools,
    initiator: Initiator,
    host: ChatHost,
    tools_by_name: dict[str, Tool],
) -> str:
    tool = tools_by_name.get(call.name)
    if tool is None:
        return json.dumps({"error": "unknown tool"})
    try:
        payload = dict(tool.execute(service, call.arguments))
        if call.name in _PLAN_TOOLS:
            payload = _confirm_plan(
                call.name, call.arguments, payload, service, agent, initiator, host
            )
        return json.dumps(payload)
    except AuditLogError:
        raise
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


def _confirm_plan(
    name: str,
    arguments: Mapping[str, Any],
    payload: dict[str, Any],
    service: PlatformService,
    agent: AgentTools,
    initiator: Initiator,
    host: ChatHost,
) -> dict[str, Any]:
    tenant_id = arguments["tenant_id"]
    if not isinstance(tenant_id, str):
        raise ConfirmationRequiredError("no plan")
    plan = agent.remembered_plan(tenant_id)
    if name == "plan_create" and isinstance(plan, CreatePlan):
        return _confirm_create(plan, payload, service, initiator, host)
    if name == "plan_update" and isinstance(plan, UpdatePlan):
        return _confirm_update(plan, payload, service, agent, initiator, host)
    if name == "plan_destroy" and isinstance(plan, DestroyPlan):
        return _confirm_destroy(plan, payload, service, initiator, host)
    raise ConfirmationRequiredError("no plan")


def _confirm_create(
    plan: CreatePlan,
    payload: dict[str, Any],
    service: PlatformService,
    initiator: Initiator,
    host: ChatHost,
) -> dict[str, Any]:
    if not host.confirm_create(plan):
        service.decline_create(plan, initiator)
        return _declined(payload)
    secret = service.confirm_create(plan, initiator)
    return _accepted(payload, secret)


def _confirm_update(
    plan: UpdatePlan,
    payload: dict[str, Any],
    service: PlatformService,
    agent: AgentTools,
    initiator: Initiator,
    host: ChatHost,
) -> dict[str, Any]:
    if plan.nothing_to_change:
        return _declined(payload)
    chosen = host.confirm_update(plan)
    if chosen is None or not _update_covers_plan(plan, chosen):
        service.decline_update(plan, initiator)
        return _declined(payload)
    agent.accept_update(plan.tenant_id, chosen)
    secret = service.confirm_update(
        plan,
        initiator,
        data_loss_confirmed=bool(plan.removals),
        confirmed_replacements=frozenset(plan.replacements),
    )
    return _accepted(payload, secret)


def _confirm_destroy(
    plan: DestroyPlan,
    payload: dict[str, Any],
    service: PlatformService,
    initiator: Initiator,
    host: ChatHost,
) -> dict[str, Any]:
    if not host.confirm_destroy(plan):
        service.decline_destroy(plan, initiator)
        return _declined(payload)
    secret = service.confirm_destroy(plan, initiator)
    return _accepted(payload, secret)


def _update_covers_plan(plan: UpdatePlan, chosen: UpdateConfirmations | None) -> bool:
    if chosen is None or not chosen.update:
        return False
    if plan.removals and not chosen.removals:
        return False
    return frozenset(plan.replacements) == chosen.replacements


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

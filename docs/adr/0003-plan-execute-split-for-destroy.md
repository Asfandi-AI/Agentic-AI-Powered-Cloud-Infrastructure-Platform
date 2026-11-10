# 0003. Plan and execute for destroy

## Status

Accepted

## Context

Requirement 10 lists one destroy tool, together with list catalog, draft manifest, validate manifest, estimate, plan create, plan update, execute create, execute update, list tenants, and tenant status. That list is eleven tools. Requirement 11 requires a confirmation token for execute create, execute update, and destroy, issued only when the user confirms that exact plan outside the model. Create and update already split the plan from the execute. A single destroy tool would both describe the deletion and perform it, so one model call could delete the tenant.

## Decision

Destroy is two tools. `plan_destroy` returns the plan and does not destroy the tenant and does not return a token. The host confirms that plan: the CLI, `chat`, or an MCP elicitation. `execute_destroy` accepts the token and is the only destroy tool that calls the executor. The audit name for that execution stays `destroy`. The agent therefore has twelve tools.

## Consequences

The model must call `plan_destroy` before `execute_destroy`. The remembered plan stays in the process and is gone when the process exits. Requirement 10's list of eleven names is not what the code offers. Clients and prompts that still send one destroy tool do not delete a tenant.

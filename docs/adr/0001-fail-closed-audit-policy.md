# 0001. Fail-closed audit log

## Status

Accepted

## Context

Requirement 13 records every agent tool call and every confirm or decline: the time, the initiator, the tool, the tenant where there is one, an argument summary, and the outcome. That record is how an operator sees who asked for what. Create, update, and destroy change the registry and the executor. If those actions can start when the audit sink is not accepting writes, the tenant can change with no entry. The live file sink is the local JSON-lines file. CloudWatch Logs is wired beside it when `SSP_AWS_REGION` and `SSP_LOG_GROUP` are both set. An invalid region or an illegal log group name is refused before the client is created.

## Decision

A failed audit write refuses to start execute or destroy. The confirmation entry is written first. A sink that raises does so before any registry write or executor call. The CLI exits 2 and prints `cannot write the audit log`. The MCP server returns JSON-RPC error `-32603` with the same text. Plan, decline, refusal, and failed-attempt entries stay in the process and do not create or change `SSP_HOME`.

If the outcome write fails after the registry has already changed, the change is left in place and the error is still raised. That later failure is reported. It is not rolled back.

## Consequences

An unavailable audit sink blocks create, update, and destroy from starting, on the CLI and through the agent. A successful mutation whose outcome line cannot be written remains in the registry, and the caller still sees the audit error. The JSON-lines file is appended only when an execute or destroy succeeds, after the state directory exists. Every file write calls `fsync`. With both variables set, that flushed batch also goes to CloudWatch Logs. Without them, only the file is used. A failed CloudWatch write still raises. The CLI and the MCP server report `cannot write the audit log` and do not print the AWS error. The original exception is chained on `AuditLogError`.

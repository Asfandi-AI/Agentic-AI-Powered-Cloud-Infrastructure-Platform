# Architecture Defense

This document explains the design of the Agentic-AI-Powered-Cloud-Infrastructure-Platform for a technical reader. The implementation is a local simulation. Bedrock and CloudWatch Logs are wired and opt-in: each one runs only when its environment variables are set. A real executor is still future work.

## Executive Summary

The platform validates a YAML manifest against a fixed catalog, estimates cost, and creates, updates, and destroys a tenant. Those operations look small. The constraints around them are not. A rejected manifest must produce no executor call and no registry write. A model must not be the thing that approves its own plan. An audit sink that cannot record a mutation must stop that mutation from starting. `core/` and `cli/` do not import a cloud SDK. Components reach the executor only as a validated manifest. Those rules are why the codebase is built from ports, a confirmation token, a fail-closed audit log, and a test suite that locks each rule in place.

The size follows from that. 7,377 lines of application Python sit behind 19,595 lines of tests, 816 of them at the last full run. Two modules carry the control plane: `PlatformService` and the CLI. The agent path repeats the same confirmation rule in the terminal and over stdio. A hiring reader who opens `core/service.py` or `cli/main.py` is looking at the place where those rules meet, not at an unfinished script that grew an interface layer for its own sake.

## The Core Abstractions (Ports & Adapters)

The boundaries that must survive a change of database, executor, or model are `typing.Protocol` types: `Registry`, `Executor`, `ModelClient`, `ChatHost`, `AuditLog`, `PlanTokenIssuer`, `PlanTokenVerifier`, and `Tool`. A protocol states the operations the core is willing to call. It does not choose SQLite, the local simulator, Bedrock, or Typer.

`Registry.transition` is a compare-and-set on tenant status. `Executor.provision` and `Executor.destroy` accept a tenant and a validated manifest, and they return nothing the core has to interpret as a cloud response. `ModelClient.complete` returns the next turn. `ChatHost` is the only way the chat loop reads a person, prints a reply, or accepts a plan. The loop does not import the CLI. `PlatformService` depends on the issuer and the verifier, and it never sees how a token is stored.

That split is what makes the suite fast and what keeps a future adapter small. Tests pass an in-memory registry, `FakeExecutor`, and `FakeModelClient`. The architecture tests fail if `core/` or `cli/` grow an import of boto3, CDK, or Terraform. A real executor implements `Executor` and receives the same `ProvisionRequest` the local one receives. `BedrockModelClient` implements `ModelClient` and is built in `wiring.py` when `SSP_BEDROCK_MODEL_ID` is set. Neither the model client nor a future executor rewrites validation, the status machine, or the confirmation check.

## The Custom MCP Server

Requirement 12 asks for the same tools, the same validation, and the same confirmation rule on stdio that the CLI already enforces in the terminal. `ssp-mcp` is a JSON-RPC session in `agent/mcp_server.py` and `mcp_main.py`. It implements `initialize`, `tools/list`, `tools/call`, and `ping` for protocol version `2025-06-18`. stdout carries JSON-RPC only. Tool calls go to `AgentTools`, and those tools call `PlatformService` only.

The direct runtime dependencies are pydantic, PyYAML, Typer, and boto3. There is no MCP library, and this process does not serve HTTP. Application code imports boto3 only from `wiring.py`. A library that also spoke stdio would still have to be wrapped so that a plan cannot execute itself. Here that rule is visible: when the client advertises elicitation, a plan sends `elicitation/create` and the service issues the token from the client's answer. When the client does not advertise elicitation, the plan is declined. The model does not mint the token. A REST wrapper around the same service would move confirmation into a route the model could call. The stdio session keeps the approver on the other side of the pipe.

The session is deliberately narrow. It does not pretend to be a general MCP framework. It speaks the methods this platform uses, and a client that needs another method is refused with method-not-found.

## Security First (Fail-Closed)

Requirement 13 is the record of who asked for what: time, initiator, tool, tenant, an argument summary, and the outcome. A create, update, or destroy that can start when that record cannot be written is an unattributed change. The service writes the confirmation entry first. If the sink raises, the exception leaves `apply_create`, `apply_update`, or `apply_destroy` before any registry write and before any executor call. The CLI exits 2 with `cannot write the audit log`. The MCP session returns JSON-RPC error `-32603` with the same text.

Silent continuation was the alternative that loses the requirement. A best-effort log lets the tenant change and leaves the operator with a gap. Fail-closed trades availability of the mutation for a complete record of the mutations that did start.

The boundary is exact. A failed outcome write after the registry has already changed does not roll the tenant back. The change stays, and the error is still raised. Plan, decline, refusal, and failed-attempt entries stay in the process and do not create `SSP_HOME`. The JSON-lines file is appended when an execute or destroy succeeds. Argument keys are allow-listed so a token, an API key, or a credential does not become a log line. When `SSP_AWS_REGION` and `SSP_LOG_GROUP` are both set, that same batch is also written to CloudWatch Logs. An invalid region or log group name is refused before the client is created. The CLI exits 2 with `cannot write the audit log` and does not print the underlying AWS error. The original exception stays chained for the developer. The policy does not depend on CloudWatch being configured. Any `AuditLog` that raises is enough to refuse the start.

## TDD & Quality

The suite is the specification you can run. A rejected manifest does not call the executor and does not write the registry. A missing or reused confirmation token does not destroy a tenant. A secret passed as an audit argument does not appear in the rendered line. Status prints options only when `status` asks for them. Create, update, and destroy do not. Each of those is an assertion against behavior, and the large files (`tests/core/test_service.py`, `tests/cli/test_update_destroy.py`) are large because the state machine and the confirmation matrix have many legal and illegal edges.

816 tests on 7,377 lines of application code is a ratio chosen so a change to the token check, the compare-and-set, or the audit allow-list fails in public. The tests run without AWS and without Bedrock. `FakeExecutor`, `FakeModelClient`, and an in-memory audit sink are the adapters the protocols were added to allow. Production-readiness here means the control plane refuses the dangerous cases deterministically. It does not mean a cloud account is already attached. The Bedrock and CloudWatch tests use fakes and do not call AWS. The same tests are what a real executor has to keep passing.

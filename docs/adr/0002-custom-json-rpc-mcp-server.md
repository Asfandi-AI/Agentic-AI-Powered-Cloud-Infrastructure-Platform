# 0002. Custom JSON-RPC MCP server

## Status

Accepted

## Context

Requirement 12 asks for an MCP server that exposes the same tools as the agent and enforces the same confirmation rule. Requirement 11 says a confirmation token is issued only when the user confirms that exact plan outside the model. The runtime dependencies are pydantic, PyYAML, and Typer. There is no `mcp` package. `ssp-mcp` must keep stdout for JSON-RPC only, and a plan must ask the client to confirm with `elicitation/create` before any execute tool runs.

## Decision

`ssp-mcp` is a stdio JSON-RPC session in `agent/mcp_server.py` and `mcp_main.py`. It implements `initialize`, `tools/list`, `tools/call`, and `ping` for protocol version `2025-06-18`. Tool calls go to `AgentTools`, which call `PlatformService` only. When the client advertises elicitation, a plan sends `elicitation/create` and the service issues the token from the client's answer. When the client does not advertise elicitation, the plan is declined. The official `mcp` package is not a dependency.

## Consequences

The server speaks the methods this platform uses and no others. Confirmation stays in this process, and the model does not issue the token. Adding the official package later would be a new dependency and would still have to keep stdout as JSON-RPC only and keep token checks inside `PlatformService`. A client that needs a method this session does not implement is not served.

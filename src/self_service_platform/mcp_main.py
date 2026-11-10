"""Stdio entry point for the MCP server. stdout carries JSON-RPC only."""

import getpass
import json
import sys
from collections.abc import Mapping
from typing import Any, NoReturn, TextIO

from self_service_platform.agent.mcp_server import McpSession
from self_service_platform.core.audit import AuditLogError
from self_service_platform.core.tenant import Initiator, InitiatorKind
from self_service_platform.wiring import build_service


def main() -> None:
    """Serve MCP on stdin and stdout until the client closes the pipe."""
    try:
        service = build_service()
    except AuditLogError:
        sys.stderr.write("cannot write the audit log\n")
        raise SystemExit(2) from None
    initiator = Initiator(kind=InitiatorKind.AGENT, owner=_owner())
    session_box: list[McpSession] = []

    def elicit(request: dict[str, Any]) -> Mapping[str, Any]:
        _write(sys.stdout, request)
        return _read_elicitation(session_box[0], request["id"])

    session_box.append(McpSession(service, initiator, elicit=elicit))
    _serve(session_box[0])


def _serve(session: McpSession) -> None:
    while True:
        message = _read_message(sys.stdin)
        if message is None:
            return
        response = session.handle(message)
        if response is not None:
            _write(sys.stdout, response)


def _read_elicitation(session: McpSession, request_id: object) -> Mapping[str, Any]:
    while True:
        message = _read_message(sys.stdin)
        if message is None:
            return {"action": "cancel"}
        if message.get("id") == request_id and "method" not in message:
            result = message.get("result")
            if isinstance(result, Mapping):
                return result
            return {"action": "decline"}
        response = session.handle(message)
        if response is not None:
            _write(sys.stdout, response)


def _read_message(stdin: TextIO) -> dict[str, Any] | None:
    while True:
        line = stdin.readline()
        if line == "":
            return None
        text = line.strip()
        if text == "":
            continue
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            _write(
                sys.stdout,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                },
            )
            continue
        if isinstance(loaded, dict):
            return loaded


def _write(stdout: TextIO, message: Mapping[str, Any]) -> None:
    stdout.write(json.dumps(message))
    stdout.write("\n")
    stdout.flush()


def _owner() -> str:
    try:
        username = getpass.getuser()
    except (OSError, KeyError, ImportError):
        _reject_user()
    if username.strip() == "":
        _reject_user()
    return username


def _reject_user() -> NoReturn:
    sys.stderr.write("cannot determine the operating system user\n")
    raise SystemExit(2)

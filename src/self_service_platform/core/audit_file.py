"""Append-only JSON-lines audit file. The constructor does not touch the disk."""

import json
import os
from pathlib import Path

from self_service_platform.core.audit import AuditEntry
from self_service_platform.statedir import create_state_directory

_LOG_NAME = "audit.jsonl"
_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)


class JsonLinesAuditLog:
    """One JSON object per line in ``audit.jsonl`` under a state directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def write(self, entry: AuditEntry) -> None:
        create_state_directory(self._directory)
        _append(self._directory / _LOG_NAME, _line(entry))


def _line(entry: AuditEntry) -> bytes:
    payload = {
        "arguments": entry.arguments,
        "decision": None if entry.decision is None else entry.decision.value,
        "initiator_kind": entry.initiator_kind.value,
        "initiator_owner": entry.initiator_owner,
        "outcome": None if entry.outcome is None else entry.outcome.value,
        "tenant_id": entry.tenant_id,
        "time": entry.time.isoformat(),
        "tool_name": entry.tool_name.value,
    }
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{text}\n".encode("ascii")


def _append(path: Path, payload: bytes) -> None:
    fd = os.open(path, _OPEN_FLAGS, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while len(view) > 0:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("audit log write returned no bytes")
        view = view[written:]

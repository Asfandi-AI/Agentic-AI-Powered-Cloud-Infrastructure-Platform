"""Append-only JSON-lines audit file. The constructor does not touch the disk."""

import os
from pathlib import Path

from self_service_platform.core.audit import AuditEntry, audit_json
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
    return f"{audit_json(entry)}\n".encode("ascii")


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

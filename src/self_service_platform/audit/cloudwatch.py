"""CloudWatch Logs audit sink. The constructor does not call AWS."""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from botocore.exceptions import ClientError

from self_service_platform.core.audit import AuditEntry, audit_json

_EXISTS = "ResourceAlreadyExistsException"
_RETRYABLE = frozenset(
    {"InvalidSequenceTokenException", "DataAlreadyAcceptedException"}
)
_LOG_GROUP_NAME = re.compile(r"^[\.\-_/#A-Za-z0-9]{1,512}$")
_INVALID_LOG_GROUP = "invalid CloudWatch log group name"


class LogsClient(Protocol):
    """The CloudWatch Logs calls this sink makes."""

    def create_log_group(self, *, logGroupName: str) -> object: ...

    def create_log_stream(
        self,
        *,
        logGroupName: str,
        logStreamName: str,
    ) -> object: ...

    def put_log_events(
        self,
        *,
        logGroupName: str,
        logStreamName: str,
        logEvents: Sequence[Mapping[str, Any]],
        sequenceToken: str | None = None,
    ) -> object: ...


class InvalidLogGroupNameError(Exception):
    """Raised when a log group name is not a CloudWatch log group name."""

    def __init__(self) -> None:
        super().__init__(_INVALID_LOG_GROUP)


def require_log_group_name(name: str) -> None:
    """Reject a name CloudWatch Logs would refuse. This does not call AWS."""
    if _LOG_GROUP_NAME.fullmatch(name) is None:
        raise InvalidLogGroupNameError


class RealCloudWatchAuditLog:
    """Appends one JSON object to a CloudWatch Logs stream."""

    def __init__(self, client: LogsClient, log_group: str, log_stream: str) -> None:
        require_log_group_name(log_group)
        self._client = client
        self.log_group = log_group
        self.log_stream = log_stream

    def write(self, entry: AuditEntry) -> None:
        _create_group(self._client, self.log_group)
        _create_stream(self._client, self.log_group, self.log_stream)
        _put_event(self._client, self.log_group, self.log_stream, entry)


def _create_group(client: LogsClient, log_group: str) -> None:
    try:
        client.create_log_group(logGroupName=log_group)
    except ClientError as exc:
        if _error_code(exc) != _EXISTS:
            raise


def _create_stream(client: LogsClient, log_group: str, log_stream: str) -> None:
    try:
        client.create_log_stream(logGroupName=log_group, logStreamName=log_stream)
    except ClientError as exc:
        if _error_code(exc) != _EXISTS:
            raise


def _put_event(
    client: LogsClient,
    log_group: str,
    log_stream: str,
    entry: AuditEntry,
    sequence_token: str | None = None,
) -> None:
    events = (_event(entry),)
    try:
        _send(client, log_group, log_stream, events, sequence_token)
    except ClientError as exc:
        if sequence_token is not None or _error_code(exc) not in _RETRYABLE:
            raise
        token = _expected_token(exc)
        if token is None:
            raise
        _put_event(client, log_group, log_stream, entry, token)


def _send(
    client: LogsClient,
    log_group: str,
    log_stream: str,
    events: tuple[Mapping[str, Any], ...],
    sequence_token: str | None,
) -> None:
    if sequence_token is None:
        client.put_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            logEvents=events,
        )
        return
    client.put_log_events(
        logGroupName=log_group,
        logStreamName=log_stream,
        logEvents=events,
        sequenceToken=sequence_token,
    )


def _event(entry: AuditEntry) -> dict[str, Any]:
    return {
        "timestamp": int(entry.time.timestamp() * 1000),
        "message": audit_json(entry),
    }


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    if not isinstance(error, Mapping):
        return ""
    code = error.get("Code")
    if isinstance(code, str):
        return code
    return ""


def _expected_token(exc: ClientError) -> str | None:
    token = exc.response.get("expectedSequenceToken")
    if isinstance(token, str):
        return token
    return None

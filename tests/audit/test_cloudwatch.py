"""CloudWatch Logs audit sink."""

import ast
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from self_service_platform.audit.cloudwatch import (
    InvalidLogGroupNameError,
    RealCloudWatchAuditLog,
)
from self_service_platform.core.audit import (
    AuditEntry,
    AuditLogError,
    CompositeAuditLog,
    Outcome,
    ToolName,
    audit_json,
)
from self_service_platform.core.audit_file import JsonLinesAuditLog
from self_service_platform.core.tenant import InitiatorKind

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "src" / "self_service_platform"
_AUDIT_PACKAGE = "self_service_platform.audit"
_GROUP = "ssp-audit-group"
_STREAM = "ssp-audit-log"
_TIME = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
_LATER = datetime(2026, 11, 6, 10, 0, 1, tzinfo=UTC)
_TIMESTAMP_MS = 1793959200000
_SECRET = "aws-secret-value"
_RUNNER = CliRunner()
_ARGUMENTS = {
    "catalog_version": None,
    "component_type": None,
    "data_loss_confirmed": None,
    "dry_run": None,
    "manifest_entry_count": None,
    "removal_count": None,
    "replacement_count": None,
}


class FakeLogsClient:
    """Records CloudWatch Logs calls. It does not touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.group_error: BaseException | None = None
        self.stream_error: BaseException | None = None
        self.put_errors: list[BaseException] = []

    def create_log_group(self, *, logGroupName: str) -> dict[str, object]:
        self.calls.append(("create_log_group", {"logGroupName": logGroupName}))
        if self.group_error is not None:
            raise self.group_error
        return {}

    def create_log_stream(
        self,
        *,
        logGroupName: str,
        logStreamName: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "create_log_stream",
                {"logGroupName": logGroupName, "logStreamName": logStreamName},
            )
        )
        if self.stream_error is not None:
            raise self.stream_error
        return {}

    def put_log_events(
        self,
        *,
        logGroupName: str,
        logStreamName: str,
        logEvents: Sequence[Mapping[str, object]],
        sequenceToken: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "logGroupName": logGroupName,
            "logStreamName": logStreamName,
            "logEvents": [dict(event) for event in logEvents],
        }
        if sequenceToken is not None:
            payload["sequenceToken"] = sequenceToken
        self.calls.append(("put_log_events", payload))
        if self.put_errors:
            raise self.put_errors.pop(0)
        return {}


def _log(client: FakeLogsClient) -> RealCloudWatchAuditLog:
    return RealCloudWatchAuditLog(client, log_group=_GROUP, log_stream=_STREAM)


def _entry(
    *,
    tool: ToolName = ToolName.EXECUTE_CREATE,
    moment: datetime = _TIME,
) -> AuditEntry:
    return AuditEntry(
        time=moment,
        initiator_kind=InitiatorKind.CLI,
        initiator_owner="owner-a",
        tool_name=tool,
        tenant_id="tenant-a",
        arguments=dict(_ARGUMENTS),
        outcome=Outcome.SUCCEEDED,
        decision=None,
    )


def _names(client: FakeLogsClient) -> list[str]:
    return [name for name, _payload in client.calls]


def _puts(client: FakeLogsClient) -> list[dict[str, object]]:
    return [payload for name, payload in client.calls if name == "put_log_events"]


def _cloudwatch_error(
    code: str,
    operation: str,
    expected_sequence_token: str | None = None,
    message: str | None = None,
) -> BaseException:
    from botocore.exceptions import ClientError

    response: dict[str, object] = {
        "Error": {"Code": code, "Message": code if message is None else message},
        "ResponseMetadata": {"HTTPStatusCode": 400},
    }
    if expected_sequence_token is not None:
        response["expectedSequenceToken"] = expected_sequence_token
    return ClientError(response, operation)


def _import_statements(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                statements.append(segment)
    return statements


def _imported_names(statement: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(statement)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _mentions_audit(name: str) -> bool:
    return name == _AUDIT_PACKAGE or name.startswith(f"{_AUDIT_PACKAGE}.")


def _audit_violations(paths: Sequence[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            if any(_mentions_audit(name) for name in _imported_names(statement)):
                found.append(f"{relative}: {statement}")
    return found


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def test_the_constructor_makes_no_client_calls() -> None:
    client = FakeLogsClient()
    _log(client)
    assert client.calls == []


def test_write_creates_the_group_and_stream_then_puts_one_event() -> None:
    client = FakeLogsClient()
    _log(client).write(_entry())

    assert _names(client) == [
        "create_log_group",
        "create_log_stream",
        "put_log_events",
    ]
    group, stream, put = (payload for _name, payload in client.calls)
    assert group == {"logGroupName": _GROUP}
    assert stream == {"logGroupName": _GROUP, "logStreamName": _STREAM}
    assert put["logGroupName"] == _GROUP
    assert put["logStreamName"] == _STREAM
    events = put["logEvents"]
    assert isinstance(events, list)
    assert len(events) == 1


def test_the_event_timestamp_is_epoch_milliseconds_and_the_message_matches() -> None:
    client = FakeLogsClient()
    entry = _entry()
    _log(client).write(entry)

    events = _puts(client)[0]["logEvents"]
    assert isinstance(events, list)
    event = events[0]
    assert isinstance(event, dict)
    assert set(event) == {"message", "timestamp"}
    assert event["timestamp"] == _TIMESTAMP_MS
    message = event["message"]
    assert isinstance(message, str)
    assert message == audit_json(entry)
    assert not message.endswith("\n")


def test_an_existing_group_and_stream_still_receive_the_event() -> None:
    client = FakeLogsClient()
    client.group_error = _cloudwatch_error(
        "ResourceAlreadyExistsException",
        "CreateLogGroup",
    )
    client.stream_error = _cloudwatch_error(
        "ResourceAlreadyExistsException",
        "CreateLogStream",
    )

    _log(client).write(_entry())

    assert _names(client) == [
        "create_log_group",
        "create_log_stream",
        "put_log_events",
    ]
    assert len(_puts(client)) == 1


def test_any_other_client_error_propagates() -> None:
    client = FakeLogsClient()
    error = _cloudwatch_error("AccessDeniedException", "CreateLogGroup")
    client.group_error = error

    with pytest.raises(type(error)) as raised:
        _log(client).write(_entry())

    assert raised.value.response["Error"]["Code"] == "AccessDeniedException"
    assert _names(client) == ["create_log_group"]


def test_an_invalid_sequence_token_retries_once() -> None:
    client = FakeLogsClient()
    client.put_errors = [
        _cloudwatch_error(
            "InvalidSequenceTokenException",
            "PutLogEvents",
            "token-2",
        )
    ]

    _log(client).write(_entry())

    puts = _puts(client)
    assert len(puts) == 2
    assert "sequenceToken" not in puts[0]
    assert puts[1]["sequenceToken"] == "token-2"

    failing = FakeLogsClient()
    second = _cloudwatch_error(
        "InvalidSequenceTokenException",
        "PutLogEvents",
        "token-3",
    )
    failing.put_errors = [
        _cloudwatch_error(
            "InvalidSequenceTokenException",
            "PutLogEvents",
            "token-2",
        ),
        second,
    ]

    with pytest.raises(type(second)) as raised:
        _log(failing).write(_entry())

    puts = _puts(failing)
    assert len(puts) == 2
    assert puts[1]["sequenceToken"] == "token-2"
    assert raised.value.response["Error"]["Code"] == "InvalidSequenceTokenException"


def test_an_invalid_sequence_token_without_a_next_token_is_raised() -> None:
    client = FakeLogsClient()
    error = _cloudwatch_error("InvalidSequenceTokenException", "PutLogEvents")
    client.put_errors = [error]

    with pytest.raises(type(error)) as raised:
        _log(client).write(_entry())

    assert len(_puts(client)) == 1
    assert "sequenceToken" not in _puts(client)[0]
    assert raised.value.response["Error"]["Code"] == "InvalidSequenceTokenException"


def test_a_data_already_accepted_error_retries_once_with_the_next_token() -> None:
    client = FakeLogsClient()
    client.put_errors = [
        _cloudwatch_error(
            "DataAlreadyAcceptedException",
            "PutLogEvents",
            "token-9",
        )
    ]

    _log(client).write(_entry())

    puts = _puts(client)
    assert len(puts) == 2
    assert "sequenceToken" not in puts[0]
    assert puts[1]["sequenceToken"] == "token-9"


def test_data_already_accepted_without_a_next_token_is_raised() -> None:
    client = FakeLogsClient()
    error = _cloudwatch_error("DataAlreadyAcceptedException", "PutLogEvents")
    client.put_errors = [error]

    with pytest.raises(type(error)) as raised:
        _log(client).write(_entry())

    assert len(_puts(client)) == 1
    assert raised.value.response["Error"]["Code"] == "DataAlreadyAcceptedException"


def test_a_legal_log_group_is_sent_and_an_illegal_one_is_refused() -> None:
    client = FakeLogsClient()
    legal = "team/app_audit-1.live#A"
    RealCloudWatchAuditLog(client, log_group=legal, log_stream=_STREAM).write(_entry())
    assert client.calls[0][1]["logGroupName"] == legal

    refused = FakeLogsClient()
    with pytest.raises(
        InvalidLogGroupNameError,
        match=r"^invalid CloudWatch log group name$",
    ):
        RealCloudWatchAuditLog(refused, log_group="bad name", log_stream=_STREAM)
    with pytest.raises(InvalidLogGroupNameError):
        RealCloudWatchAuditLog(refused, log_group="log:group", log_stream=_STREAM)
    with pytest.raises(InvalidLogGroupNameError):
        RealCloudWatchAuditLog(refused, log_group="a" * 513, log_stream=_STREAM)
    assert refused.calls == []


def test_a_second_write_puts_a_second_event() -> None:
    client = FakeLogsClient()
    log = _log(client)
    first = _entry()
    second = _entry(tool=ToolName.DESTROY, moment=_LATER)

    log.write(first)
    log.write(second)

    puts = _puts(client)
    assert len(puts) == 2
    assert puts[0]["logEvents"] == [
        {"timestamp": _TIMESTAMP_MS, "message": audit_json(first)}
    ]
    assert puts[1]["logEvents"] == [
        {"timestamp": _TIMESTAMP_MS + 1000, "message": audit_json(second)}
    ]


def test_wiring_uses_cloudwatch_only_when_region_and_group_are_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    calls: list[tuple[str, str]] = []

    def fake_client(service_name: str, *, region_name: str) -> FakeLogsClient:
        calls.append((service_name, region_name))
        return FakeLogsClient()

    monkeypatch.setattr(boto3, "client", fake_client)
    from self_service_platform.wiring import build_service

    monkeypatch.setenv("SSP_HOME", str(tmp_path))
    monkeypatch.setenv("SSP_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SSP_LOG_GROUP", "platform-audit")
    configured = build_service()
    sink = configured._audit_log._sink
    assert isinstance(sink, CompositeAuditLog)
    assert len(sink._sinks) == 2
    assert isinstance(sink._sinks[0], JsonLinesAuditLog)
    cloud = sink._sinks[1]
    assert isinstance(cloud, RealCloudWatchAuditLog)
    assert cloud.log_group == "platform-audit"
    assert cloud.log_stream == "ssp-audit-log"
    assert calls == [("logs", "eu-west-1")]

    def assert_file_only() -> None:
        calls.clear()
        built = build_service()
        assert isinstance(built._audit_log._sink, JsonLinesAuditLog)
        assert calls == []

    monkeypatch.delenv("SSP_AWS_REGION")
    assert_file_only()

    monkeypatch.setenv("SSP_AWS_REGION", "eu-west-1")
    monkeypatch.delenv("SSP_LOG_GROUP")
    assert_file_only()

    monkeypatch.setenv("SSP_AWS_REGION", "")
    monkeypatch.setenv("SSP_LOG_GROUP", "platform-audit")
    assert_file_only()

    monkeypatch.setenv("SSP_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SSP_LOG_GROUP", "   ")
    assert_file_only()


def test_wiring_rejects_an_illegal_log_group_before_creating_a_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    calls: list[tuple[str, str]] = []

    def fake_client(service_name: str, *, region_name: str) -> FakeLogsClient:
        calls.append((service_name, region_name))
        return FakeLogsClient()

    monkeypatch.setattr(boto3, "client", fake_client)
    illegal = f"{_SECRET} leaked"
    monkeypatch.setenv("SSP_HOME", str(tmp_path))
    monkeypatch.setenv("SSP_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SSP_LOG_GROUP", illegal)
    from self_service_platform.wiring import build_service

    with pytest.raises(AuditLogError, match=r"^1 audit sink failed$") as raised:
        build_service()

    cause = raised.value.__cause__
    assert isinstance(cause, InvalidLogGroupNameError)
    assert calls == []
    assert illegal not in str(raised.value)
    assert illegal not in str(cause)


def test_wiring_rejects_an_invalid_region_before_creating_a_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    calls: list[tuple[str, str]] = []

    def fake_client(service_name: str, *, region_name: str) -> FakeLogsClient:
        calls.append((service_name, region_name))
        return FakeLogsClient()

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("SSP_HOME", str(tmp_path))
    monkeypatch.setenv("SSP_AWS_REGION", "not-a-region")
    monkeypatch.setenv("SSP_LOG_GROUP", "platform-audit")
    from self_service_platform.wiring import build_service

    with pytest.raises(AuditLogError, match=r"^1 audit sink failed$") as raised:
        build_service()

    assert calls == []
    assert str(raised.value.__cause__) == "invalid AWS region"
    assert "not-a-region" not in str(raised.value)


def test_the_cli_hides_an_illegal_log_group_and_a_cloudwatch_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    from self_service_platform.app import app

    calls: list[tuple[str, str]] = []

    def fake_client(service_name: str, *, region_name: str) -> FakeLogsClient:
        calls.append((service_name, region_name))
        client = FakeLogsClient()
        client.group_error = _cloudwatch_error(
            "InvalidParameterException",
            "CreateLogGroup",
            message=_SECRET,
        )
        return client

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("SSP_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SSP_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("SSP_BEDROCK_REGION", raising=False)
    command = ["create", "demo", "--type", "dev", "--owner", "alice", "--yes"]

    monkeypatch.setenv("SSP_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SSP_LOG_GROUP", "bad name")
    refused = _RUNNER.invoke(app, command)

    assert refused.exit_code == 2
    assert refused.stderr == "cannot write the audit log\n"
    assert refused.stdout == ""
    assert calls == []
    assert "bad name" not in refused.stderr
    assert "Traceback" not in refused.stderr

    monkeypatch.setenv("SSP_LOG_GROUP", "platform-audit")
    failed = _RUNNER.invoke(app, command)
    rendered = f"{failed.stdout}\n{failed.stderr}"

    assert failed.exit_code == 2
    assert failed.stderr == "cannot write the audit log\n"
    assert calls == [("logs", "eu-west-1")]
    assert _SECRET not in rendered
    assert "Traceback" not in rendered
    assert "InvalidParameterException" not in rendered


def test_core_and_cli_cannot_import_the_audit_package() -> None:
    assert _audit_violations(_python_files(_PACKAGE / "core")) == []
    assert _audit_violations(_python_files(_PACKAGE / "cli")) == []

    core = _PACKAGE / "core" / "_architecture_audit_boundary.py"
    cli = _PACKAGE / "cli" / "_architecture_audit_boundary.py"
    try:
        core.write_text("import self_service_platform.audit\n", encoding="utf-8")
        cli.write_text(
            "import self_service_platform.audit.cloudwatch\n",
            encoding="utf-8",
        )
        violations = _audit_violations((core, cli))
    finally:
        core.unlink(missing_ok=True)
        cli.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/core/_architecture_audit_boundary.py: "
            "import self_service_platform.audit"
        ),
        (
            "src/self_service_platform/cli/_architecture_audit_boundary.py: "
            "import self_service_platform.audit.cloudwatch"
        ),
    ]

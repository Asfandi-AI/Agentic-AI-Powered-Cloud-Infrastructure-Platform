"""Audit log tests.

Time is ISO 8601 UTC: YYYY-MM-DDTHH:MM:SS+00:00, the result of
datetime.isoformat() on a UTC instant. The offset is +00:00, not Z.
Microseconds are a dot and six digits before the offset, and only when
they are non-zero.
"""

import json
import multiprocessing
import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from self_service_platform.core.audit import (
    AuditEntry,
    AuditLog,
    AuditLogError,
    CompositeAuditLog,
    Decision,
    InvalidAuditArgumentError,
    Outcome,
    ToolName,
    record_plan_decision,
    record_tool_call,
)
from self_service_platform.core.audit_cloudwatch import FakeCloudWatchLogs
from self_service_platform.core.audit_file import JsonLinesAuditLog
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    RegistryUnavailableError,
)

_START = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
_TIME = "2026-11-06T10:00:00+00:00"
_CATALOG_VERSION = "ab" * 32
_TOKEN = "confirmation-token-example"
_SECRET = "aws-secret-access-key-EXAMPLE"
_LOG_NAME = "audit.jsonl"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _initiator(owner: str = "owner-a") -> Initiator:
    return Initiator(kind=InitiatorKind.CLI, owner=owner)


def _log_path(directory: Path) -> Path:
    return directory / _LOG_NAME


def _tool(
    log: AuditLog,
    *,
    tool: ToolName = ToolName.EXECUTE_CREATE,
    tenant_id: str | None = "tenant-a",
    arguments: Mapping[str, object] | None = None,
    outcome: Outcome = Outcome.SUCCEEDED,
    owner: str = "owner-a",
    clock: _Clock | None = None,
) -> None:
    record_tool_call(
        log,
        clock or _Clock(_START),
        initiator=_initiator(owner),
        tool=tool,
        tenant_id=tenant_id,
        arguments={} if arguments is None else arguments,
        outcome=outcome,
    )


def _decision(
    log: AuditLog,
    *,
    tool: ToolName = ToolName.EXECUTE_CREATE,
    tenant_id: str | None = "tenant-a",
    decision: Decision = Decision.CONFIRMED,
    arguments: Mapping[str, object] | None = None,
    owner: str = "owner-a",
) -> None:
    record_plan_decision(
        log,
        _Clock(_START),
        initiator=_initiator(owner),
        tool=tool,
        tenant_id=tenant_id,
        decision=decision,
        arguments=arguments,
    )


def _lines(directory: Path) -> list[dict[str, object]]:
    text = _log_path(directory).read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def _as_json(entry: AuditEntry) -> dict[str, object]:
    arguments = entry.arguments
    if hasattr(arguments, "model_dump"):
        dumped = arguments.model_dump(mode="json")
    else:
        dumped = dict(arguments)
    decision = entry.decision
    outcome = entry.outcome
    return {
        "arguments": dumped,
        "decision": None if decision is None else decision.value,
        "initiator_kind": entry.initiator_kind.value,
        "initiator_owner": entry.initiator_owner,
        "outcome": None if outcome is None else outcome.value,
        "tenant_id": entry.tenant_id,
        "time": entry.time.isoformat(),
        "tool_name": entry.tool_name.value,
    }


def _arguments() -> dict[str, object]:
    return {
        "catalog_version": _CATALOG_VERSION,
        "component_type": "s3-bucket",
        "manifest_entry_count": 2,
    }


def _expected_arguments() -> dict[str, object]:
    return {
        "catalog_version": _CATALOG_VERSION,
        "component_type": "s3-bucket",
        "data_loss_confirmed": None,
        "dry_run": None,
        "manifest_entry_count": 2,
        "removal_count": None,
        "replacement_count": None,
    }


def test_a_tool_entry_records_the_fixed_fields(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), arguments=_arguments())

    parsed = _lines(directory)
    assert len(parsed) == 1
    assert parsed[0] == {
        "arguments": _expected_arguments(),
        "decision": None,
        "initiator_kind": "cli",
        "initiator_owner": "owner-a",
        "outcome": "succeeded",
        "tenant_id": "tenant-a",
        "time": _TIME,
        "tool_name": "execute_create",
    }


def test_list_catalog_stores_a_null_tenant_id(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(
        JsonLinesAuditLog(directory),
        tool=ToolName.LIST_CATALOG,
        tenant_id=None,
        arguments={},
    )

    parsed = _lines(directory)
    assert parsed[0]["tenant_id"] is None
    assert parsed[0]["tool_name"] == "list_catalog"
    assert parsed[0]["arguments"] == {
        "catalog_version": None,
        "component_type": None,
        "data_loss_confirmed": None,
        "dry_run": None,
        "manifest_entry_count": None,
        "removal_count": None,
        "replacement_count": None,
    }


def test_a_confirmed_execute_create_records_the_decision(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _decision(JsonLinesAuditLog(directory))

    assert _lines(directory)[0]["decision"] == "confirmed"
    assert _lines(directory)[0]["outcome"] is None
    assert _lines(directory)[0]["arguments"] == {}
    assert _lines(directory)[0]["tool_name"] == "execute_create"


def test_a_declined_destroy_records_the_decision(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _decision(
        JsonLinesAuditLog(directory),
        tool=ToolName.DESTROY,
        decision=Decision.DECLINED,
    )

    assert _lines(directory)[0]["decision"] == "declined"
    assert _lines(directory)[0]["tool_name"] == "destroy"
    assert _lines(directory)[0]["arguments"] == {}


def test_a_decision_for_list_catalog_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _decision(JsonLinesAuditLog(directory), tool=ToolName.LIST_CATALOG)

    assert not _log_path(directory).exists()


def test_a_decision_with_arguments_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _decision(
            JsonLinesAuditLog(directory),
            arguments={"manifest_entry_count": 1},
        )

    assert not _log_path(directory).exists()


def test_an_unknown_argument_name_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"api_key": _SECRET})

    assert not _log_path(directory).exists()


def test_a_string_removal_count_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"removal_count": "1"})

    assert not _log_path(directory).exists()


def test_a_boolean_manifest_entry_count_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"manifest_entry_count": True})

    assert not _log_path(directory).exists()


def test_a_negative_removal_count_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"removal_count": -1})

    assert not _log_path(directory).exists()


@pytest.mark.parametrize(
    "value",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_a_catalog_version_with_the_wrong_shape_is_rejected(
    tmp_path: Path,
    value: str,
) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"catalog_version": value})

    assert not _log_path(directory).exists()


def test_a_component_type_with_an_uppercase_letter_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"component_type": "S3-bucket"})

    assert not _log_path(directory).exists()


def test_a_real_shaped_confirmation_token_is_rejected_as_a_component_type(
    tmp_path: Path,
) -> None:
    token = secrets.token_urlsafe(32)
    while "_" not in token and not any(character.isupper() for character in token):
        token = secrets.token_urlsafe(32)
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), arguments={"component_type": token})

    assert not _log_path(directory).exists()


def test_a_tenant_id_is_capped_at_128_characters(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    sink = JsonLinesAuditLog(directory)
    _tool(sink, tenant_id="t" * 128)

    assert _lines(directory)[0]["tenant_id"] == "t" * 128

    with pytest.raises(InvalidAuditArgumentError):
        _tool(sink, tenant_id="t" * 129)

    assert len(_lines(directory)) == 1


def test_an_agent_initiator_is_recorded(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    record_tool_call(
        JsonLinesAuditLog(directory),
        _Clock(_START),
        initiator=Initiator(kind=InitiatorKind.AGENT, owner="owner-a"),
        tool=ToolName.LIST_CATALOG,
        tenant_id=None,
        arguments={},
        outcome=Outcome.SUCCEEDED,
    )

    assert _lines(directory)[0]["initiator_kind"] == "agent"


def test_an_initiator_owner_is_capped_at_128_characters(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    sink = JsonLinesAuditLog(directory)
    _tool(sink, owner="o" * 128)

    assert _lines(directory)[0]["initiator_owner"] == "o" * 128

    with pytest.raises(InvalidAuditArgumentError):
        _tool(sink, owner="o" * 129)

    assert len(_lines(directory)) == 1


@pytest.mark.parametrize("character", ["\n", "\t", "\x00", "\x7f"])
def test_a_tenant_id_with_a_control_character_is_rejected(
    tmp_path: Path,
    character: str,
) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), tenant_id=f"tenant{character}")

    assert not _log_path(directory).exists()


@pytest.mark.parametrize("character", ["\n", "\t", "\x00", "\x7f"])
def test_an_initiator_owner_with_a_control_character_is_rejected(
    tmp_path: Path,
    character: str,
) -> None:
    directory = tmp_path / "state"
    with pytest.raises(InvalidAuditArgumentError):
        _tool(JsonLinesAuditLog(directory), owner=f"owner{character}")

    assert not _log_path(directory).exists()


def test_extra_argument_keys_and_extra_write_parameters_are_rejected(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "state"
    file_sink = JsonLinesAuditLog(directory)
    fake = FakeCloudWatchLogs("audit-group")
    composite = CompositeAuditLog((file_sink, fake))
    poisoned = {"manifest_entry_count": 1, "token": _TOKEN, "api_key": _SECRET}
    for sink in (file_sink, fake, composite):
        with pytest.raises(InvalidAuditArgumentError):
            _tool(sink, arguments=poisoned)
        with pytest.raises(InvalidAuditArgumentError):
            _decision(sink, arguments={_TOKEN: _SECRET, "api_key": _SECRET})

    assert not _log_path(directory).exists()
    assert fake.entries == []

    clean = FakeCloudWatchLogs("audit-group")
    _tool(clean, arguments={"manifest_entry_count": 1})
    entry = clean.entries[0]
    for sink in (file_sink, fake, composite):
        with pytest.raises(TypeError):
            sink.write(entry, token=_TOKEN, api_key=_SECRET)  # type: ignore[call-arg]

    rendered = str(clean.entries)
    assert _TOKEN not in rendered
    assert _SECRET not in rendered
    assert not _log_path(directory).exists()
    assert fake.entries == []


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 6, 10, 0),
        datetime(2026, 11, 6, 10, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_a_clock_that_is_not_utc_is_rejected(tmp_path: Path, moment: datetime) -> None:
    directory = tmp_path / "state"
    with pytest.raises(ValueError):
        _tool(JsonLinesAuditLog(directory), clock=_Clock(moment))

    assert not directory.exists()


def test_the_written_time_comes_from_the_injected_clock(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    clock = _Clock(_START + timedelta(seconds=5))
    _tool(JsonLinesAuditLog(directory), clock=clock)

    assert _lines(directory)[0]["time"] == "2026-11-06T10:00:05+00:00"


def test_a_refused_outcome_is_stored(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), outcome=Outcome.REFUSED)

    assert _lines(directory)[0]["outcome"] == "refused"
    assert _lines(directory)[0]["decision"] is None


def test_a_failed_outcome_stores_no_error_message(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), outcome=Outcome.FAILED)

    parsed = _lines(directory)[0]
    assert parsed["outcome"] == "failed"
    assert "message" not in parsed
    assert "error" not in parsed


def _entry() -> AuditEntry:
    return AuditEntry(
        time=_START,
        initiator_kind=InitiatorKind.CLI,
        initiator_owner="owner-a",
        tool_name=ToolName.EXECUTE_CREATE,
        tenant_id="tenant-a",
        arguments={
            "catalog_version": None,
            "component_type": None,
            "data_loss_confirmed": None,
            "dry_run": None,
            "manifest_entry_count": None,
            "removal_count": None,
            "replacement_count": None,
        },
        outcome=Outcome.SUCCEEDED,
        decision=None,
    )


def test_an_audit_entry_rejects_an_unknown_field() -> None:
    created = AuditEntry(
        time=_START,
        initiator_kind=InitiatorKind.CLI,
        initiator_owner="owner-a",
        tool_name=ToolName.EXECUTE_CREATE,
        tenant_id="tenant-a",
        arguments={
            "catalog_version": None,
            "component_type": None,
            "data_loss_confirmed": None,
            "dry_run": None,
            "manifest_entry_count": None,
            "removal_count": None,
            "replacement_count": None,
        },
        outcome=Outcome.SUCCEEDED,
        decision=None,
    )
    assert created.tenant_id == "tenant-a"

    with pytest.raises(ValidationError):
        AuditEntry(
            time=_START,
            initiator_kind=InitiatorKind.CLI,
            initiator_owner="owner-a",
            tool_name=ToolName.EXECUTE_CREATE,
            tenant_id="tenant-a",
            arguments={
                "catalog_version": None,
                "component_type": None,
                "data_loss_confirmed": None,
                "dry_run": None,
                "manifest_entry_count": None,
                "removal_count": None,
                "replacement_count": None,
            },
            outcome=Outcome.SUCCEEDED,
            decision=None,
            token=_TOKEN,
        )


def test_an_audit_entry_is_immutable() -> None:
    entry = _entry()
    with pytest.raises(ValidationError):
        entry.tenant_id = "tenant-b"


def test_two_writes_append_two_complete_lines(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    sink = JsonLinesAuditLog(directory)
    _tool(sink, tenant_id="tenant-a")
    first = _lines(directory)[0]
    _tool(sink, tenant_id="tenant-b")

    parsed = _lines(directory)
    assert parsed[0] == first
    assert [line["tenant_id"] for line in parsed] == ["tenant-a", "tenant-b"]


def test_two_sink_objects_append_to_the_same_file(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), tenant_id="tenant-a")
    _tool(JsonLinesAuditLog(directory), tenant_id="tenant-b")

    assert [line["tenant_id"] for line in _lines(directory)] == ["tenant-a", "tenant-b"]


def test_the_raw_line_is_canonical_json(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), arguments=_arguments())
    raw = _log_path(directory).read_bytes()
    line = raw.split(b"\n", 1)[0]

    assert line.decode("ascii") == json.dumps(
        json.loads(line),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def test_non_ascii_characters_stay_on_one_line(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    tenant_id = "id-\u00e9\u2028"
    _tool(JsonLinesAuditLog(directory), tenant_id=tenant_id)
    raw = _log_path(directory).read_bytes()

    assert raw.count(b"\n") == 1
    assert raw.endswith(b"\n")
    assert "é".encode() not in raw
    assert "\u2028".encode("utf-8") not in raw
    assert b"\\u00e9" in raw
    assert b"\\u2028" in raw
    assert json.loads(raw.decode("ascii"))["tenant_id"] == tenant_id


def test_the_audit_file_contains_no_carriage_return(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), tenant_id="id-\u00e9")

    assert b"\r" not in _log_path(directory).read_bytes()


def test_each_write_fsyncs_the_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each fsync waits for that line to reach disk before write returns.
    calls: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    sink = JsonLinesAuditLog(tmp_path / "state")
    _tool(sink, tenant_id="tenant-a")
    _tool(sink, tenant_id="tenant-b")

    assert len(calls) == 2


def test_a_short_os_write_still_produces_one_complete_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write

    def write_one_byte(fd: int, data: bytes) -> int:
        if len(data) == 0:
            return 0
        return real_write(fd, data[:1])

    monkeypatch.setattr(os, "write", write_one_byte)
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory), arguments=_arguments())

    text = _log_path(directory).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert json.loads(text)["arguments"] == _expected_arguments()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_state_directory_is_user_only(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    _tool(JsonLinesAuditLog(directory))

    assert directory.stat().st_mode & 0o777 == 0o700


def test_a_file_parent_writes_no_audit_file(tmp_path: Path) -> None:
    parent = tmp_path / "state"
    parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RegistryUnavailableError):
        _tool(JsonLinesAuditLog(parent))

    assert parent.is_file()
    assert parent.read_text(encoding="utf-8") == "not a directory"


def test_the_fake_records_entries_in_order() -> None:
    fake = FakeCloudWatchLogs("audit-group")
    _tool(fake, tenant_id="tenant-a")
    _tool(fake, tenant_id="tenant-b")

    assert fake.log_group == "audit-group"
    assert [entry.tenant_id for entry in fake.entries] == ["tenant-a", "tenant-b"]


def test_the_composite_writes_the_same_entry_to_each_sink(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    fake = FakeCloudWatchLogs("audit-group")
    composite = CompositeAuditLog((JsonLinesAuditLog(directory), fake))
    _tool(composite, arguments=_arguments())

    parsed = _lines(directory)
    assert len(fake.entries) == 1
    assert parsed == [_as_json(fake.entries[0])]


def test_the_audit_log_error_message_contains_no_entry_content(tmp_path: Path) -> None:
    class _Down:
        def write(self, entry: AuditEntry) -> None:
            raise RuntimeError(entry.tenant_id)

    directory = tmp_path / "state"
    composite = CompositeAuditLog((_Down(), JsonLinesAuditLog(directory)))
    with pytest.raises(AuditLogError) as raised:
        _tool(
            composite,
            tenant_id="tenant-marker",
            owner="owner-marker",
            arguments=_arguments(),
        )

    message = str(raised.value)
    assert message == "1 audit sink failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "tenant-marker" not in message
    assert "owner-marker" not in message
    assert _CATALOG_VERSION not in message
    assert _lines(directory)[0]["tenant_id"] == "tenant-marker"


def test_the_composite_attempts_the_second_sink_when_the_first_raises() -> None:
    order: list[str] = []

    class _Down:
        def write(self, entry: AuditEntry) -> None:
            order.append("first")
            raise RuntimeError("down")

    class _Second:
        def __init__(self) -> None:
            self.entries: list[AuditEntry] = []

        def write(self, entry: AuditEntry) -> None:
            order.append("second")
            self.entries.append(entry)

    second = _Second()
    composite = CompositeAuditLog((_Down(), second))
    with pytest.raises(AuditLogError) as raised:
        _tool(composite, tenant_id="tenant-a")

    assert order == ["first", "second"]
    assert len(second.entries) == 1
    assert str(raised.value) == "1 audit sink failed"


def test_the_composite_raises_when_every_sink_raises() -> None:
    order: list[str] = []

    class _Down:
        def __init__(self, name: str) -> None:
            self.name = name

        def write(self, entry: AuditEntry) -> None:
            order.append(self.name)
            raise RuntimeError(self.name)

    composite = CompositeAuditLog((_Down("first"), _Down("second")))
    with pytest.raises(AuditLogError) as raised:
        _tool(composite)

    assert order == ["first", "second"]
    assert str(raised.value) == "2 audit sinks failed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_new_file_is_created_with_mode_0o600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "state"
    target = _log_path(directory)
    real_os_chmod = os.chmod
    real_path_chmod = Path.chmod

    def os_chmod(path: os.PathLike[str] | str, mode: int, **kwargs: object) -> None:
        if Path(path) == target:
            raise AssertionError("chmod on the audit file")
        real_os_chmod(path, mode, **kwargs)

    def path_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == target:
            raise AssertionError("chmod on the audit file")
        real_path_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", os_chmod)
    monkeypatch.setattr(Path, "chmod", path_chmod)
    previous = os.umask(0)
    try:
        _tool(JsonLinesAuditLog(directory))
    finally:
        os.umask(previous)

    assert target.stat().st_mode & 0o777 == 0o600


def _write_tenant(directory: str, tenant_id: str) -> None:
    record_tool_call(
        JsonLinesAuditLog(Path(directory)),
        _Clock(_START),
        initiator=_initiator(),
        tool=ToolName.TENANT_STATUS,
        tenant_id=tenant_id,
        arguments={},
        outcome=Outcome.SUCCEEDED,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_two_processes_each_append_one_complete_line(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_tenant, args=(str(directory), tenant_id))
        for tenant_id in ("tenant-a", "tenant-b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    parsed = _lines(directory)
    assert {line["tenant_id"] for line in parsed} == {"tenant-a", "tenant-b"}
    assert len(parsed) == 2

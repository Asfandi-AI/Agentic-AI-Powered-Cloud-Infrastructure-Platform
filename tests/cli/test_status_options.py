import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from typer.testing import CliRunner

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.cli.main import _escape_stored, create_app
from self_service_platform.core.audit import AuditEntry
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.manifest import ManifestEntry
from self_service_platform.core.registry import Clock
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
)
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.registry.sqlite import SqliteRegistry

runner = CliRunner()
_T = TypeVar("_T")
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
_EXPIRES = "2026-10-17T09:00:00+00:00"
_HEADERS = (
    "status: active",
    "tenant_type: dev",
    "owner: alice",
    "initiator: cli bob",
    "catalog_version: catalog-1",
    f"expires_at: {_EXPIRES}",
    "last_error: -",
)
_VERSIONED_ON = """\
- type: versioned-bucket
  name: uploads
  options:
    versioning: true
"""
_VERSIONED_OFF = """\
- type: versioned-bucket
  name: uploads
  options:
    versioning: false
"""


class _EmptyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SortedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zeta: str
    alpha: str


class _TypedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    count: int
    ratio: float
    enabled: bool
    disabled: bool
    note: str | None


class _NestedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: dict[str, Any]


class _EscapedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str = Field(alias="a\n\x1b")


class _VersionedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versioning: bool = False


def _clock() -> datetime:
    return T0


def _option_line(key: str, value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return f"  {_escape_stored(key)}: {rendered}"


def _record(manifest: tuple[ManifestEntry, ...]) -> TenantRecord:
    return TenantRecord(
        tenant_id="aaa",
        tenant_type=TenantType.DEV,
        owner="alice",
        status=TenantStatus.ACTIVE,
        manifest=manifest,
        catalog_version="catalog-1",
        initiator=Initiator(kind=InitiatorKind.CLI, owner="bob"),
        expires_at=T0,
        last_error=None,
        updated_at=T0,
    )


def _store(path: Path, record: TenantRecord) -> None:
    with SqliteRegistry(path, _clock) as registry:
        creating = record.model_copy(update={"status": TenantStatus.CREATING})
        registry.create_tenant(creating)
        registry.transition(
            record.tenant_id,
            TenantStatus.CREATING,
            TenantStatus.ACTIVE,
            TransitionFields(
                manifest=record.manifest,
                catalog_version=record.catalog_version,
                initiator=record.initiator,
                expires_at=record.expires_at,
                last_error=record.last_error,
            ),
        )


def _status(tmp_path: Path, manifest: tuple[ManifestEntry, ...]) -> str:
    database = tmp_path / "tenants.sqlite"
    _store(database, _record(manifest))
    app = create_app(
        Catalog(),
        open_registry=lambda: SqliteRegistry.open_read_only(database),
    )
    result = runner.invoke(app, ["status", "aaa"])
    assert result.exit_code == 0
    return result.stdout


def _versioned_bucket() -> Component:
    return Component(
        component_type="versioned-bucket",
        description="A bucket",
        aws_service=AwsService.S3,
        options_model=_VersionedOptions,
        cost_line_items=(CostLineItem(description="storage", amount=None),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=("s3:CreateBucket",),
    )


def _versioned_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(_versioned_bucket())
    return catalog


class _ReopeningRegistry:
    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock

    def create_tenant(self, record: TenantRecord) -> None:
        self._call(lambda registry: registry.create_tenant(record))

    def get(self, tenant_id: str) -> TenantRecord:
        return self._call(lambda registry: registry.get(tenant_id))

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return self._call(lambda registry: registry.list_tenants())

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        return self._call(
            lambda registry: registry.transition(tenant_id, expected, new, fields)
        )

    def close(self) -> None:
        return

    def _call(self, work: Callable[[SqliteRegistry], _T]) -> _T:
        registry = SqliteRegistry(self._path, self._clock)
        try:
            return work(registry)
        finally:
            registry.close()


class _Audit:
    def write(self, entry: AuditEntry) -> None:
        return


class _Cli:
    def __init__(self, home: Path, catalog: Catalog) -> None:
        def clock() -> datetime:
            return datetime.now(UTC)

        registry = _ReopeningRegistry(home / "tenants.sqlite", clock)
        issuer, verifier = confirmation_pair(clock)
        self.service = PlatformService(
            catalog,
            registry,
            LocalExecutor(home / "local-executor.sqlite"),
            clock,
            TtlPolicy(dev=timedelta(days=1), ephemeral=timedelta(hours=3)),
            audit_log=_Audit(),
            issuer=issuer,
            verifier=verifier,
        )
        self.app = create_app(
            catalog,
            open_registry=lambda: registry,
            open_service=lambda: self.service,
        )

    def close(self) -> None:
        return


def _invoke(cli: _Cli, args: list[str]) -> tuple[int, list[str]]:
    result = runner.invoke(cli.app, args)
    return result.exit_code, result.stdout.splitlines()


def test_status_with_empty_options_prints_only_the_component_line(
    tmp_path: Path,
) -> None:
    options = _EmptyOptions().model_dump()
    stdout = _status(
        tmp_path,
        (ManifestEntry(type="s3-bucket", name="uploads", options=options),),
    )

    assert stdout.splitlines()[-1] == "uploads s3-bucket"


def test_status_prints_options_sorted_under_the_component_line(
    tmp_path: Path,
) -> None:
    options = _SortedOptions(zeta="z", alpha="a").model_dump()
    stdout = _status(
        tmp_path,
        (
            ManifestEntry(type="s3-bucket", name="uploads", options=options),
            ManifestEntry(
                type="sqs-queue",
                name="jobs",
                options=_EmptyOptions().model_dump(),
            ),
        ),
    )
    lines = stdout.splitlines()
    uploads = lines.index("uploads s3-bucket")

    assert lines[uploads + 1 : uploads + 4] == [
        _option_line("alpha", "a"),
        _option_line("zeta", "z"),
        "jobs sqs-queue",
    ]


def test_status_prints_json_for_string_number_boolean_and_null(
    tmp_path: Path,
) -> None:
    options = _TypedOptions(
        label="hello",
        count=2,
        ratio=1.5,
        enabled=True,
        disabled=False,
        note=None,
    ).model_dump()
    stdout = _status(
        tmp_path,
        (ManifestEntry(type="s3-bucket", name="uploads", options=options),),
    )
    lines = stdout.splitlines()
    uploads = lines.index("uploads s3-bucket")
    expected = [_option_line(key, options[key]) for key in sorted(options)]
    end = uploads + 1 + len(expected)

    assert lines[uploads + 1 : end] == expected


def test_status_prints_a_nested_object_as_one_json_line(tmp_path: Path) -> None:
    options = _NestedOptions(meta={"b": 1, "a": "x"}).model_dump()
    stdout = _status(
        tmp_path,
        (ManifestEntry(type="s3-bucket", name="uploads", options=options),),
    )
    lines = stdout.splitlines()
    uploads = lines.index("uploads s3-bucket")

    assert lines[uploads + 1 : uploads + 2] == [_option_line("meta", options["meta"])]
    assert lines[uploads + 2 : uploads + 3] == []


def test_status_keeps_newline_and_escape_in_options_on_one_ascii_line(
    tmp_path: Path,
) -> None:
    options = _EscapedOptions(text="c\n\x1b").model_dump(by_alias=True)
    stdout = _status(
        tmp_path,
        (ManifestEntry(type="s3-bucket", name="uploads", options=options),),
    )
    lines = stdout.splitlines()
    uploads = lines.index("uploads s3-bucket")
    option_line = _option_line("a\n\x1b", "c\n\x1b")

    assert lines[uploads + 1 : uploads + 2] == [option_line]
    assert "\n" not in option_line
    assert "\x1b" not in option_line
    stdout.encode("ascii")


def test_status_headers_stay_unchanged_when_options_are_present(
    tmp_path: Path,
) -> None:
    options = _SortedOptions(zeta="z", alpha="a").model_dump()
    stdout = _status(
        tmp_path,
        (ManifestEntry(type="s3-bucket", name="uploads", options=options),),
    )

    assert tuple(stdout.splitlines()[:7]) == _HEADERS


def test_create_with_options_prints_no_option_lines(tmp_path: Path) -> None:
    cli = _Cli(tmp_path, _versioned_catalog())
    manifest = tmp_path / "create.yaml"
    manifest.write_text(_VERSIONED_ON, encoding="utf-8")
    try:
        exit_code, lines = _invoke(
            cli,
            [
                "create",
                "demo",
                str(manifest),
                "--type",
                "dev",
                "--owner",
                "alice",
                "--yes",
            ],
        )
    finally:
        cli.close()

    assert exit_code == 0
    assert "uploads versioned-bucket" in lines
    assert _option_line("versioning", True) not in lines


def test_update_with_options_prints_no_option_lines(tmp_path: Path) -> None:
    cli = _Cli(tmp_path, _versioned_catalog())
    created = tmp_path / "create.yaml"
    updated = tmp_path / "update.yaml"
    created.write_text(_VERSIONED_ON, encoding="utf-8")
    updated.write_text(_VERSIONED_OFF, encoding="utf-8")
    try:
        created_code, _created_lines = _invoke(
            cli,
            [
                "create",
                "demo",
                str(created),
                "--type",
                "dev",
                "--owner",
                "alice",
                "--yes",
            ],
        )
        exit_code, lines = _invoke(cli, ["update", "demo", str(updated), "--yes"])
    finally:
        cli.close()

    assert created_code == 0
    assert exit_code == 0
    assert "uploads versioned-bucket" in lines
    assert _option_line("versioning", False) not in lines


def test_destroy_with_options_prints_no_option_lines(tmp_path: Path) -> None:
    cli = _Cli(tmp_path, _versioned_catalog())
    manifest = tmp_path / "create.yaml"
    manifest.write_text(_VERSIONED_ON, encoding="utf-8")
    try:
        created_code, _created_lines = _invoke(
            cli,
            [
                "create",
                "demo",
                str(manifest),
                "--type",
                "dev",
                "--owner",
                "alice",
                "--yes",
            ],
        )
        exit_code, lines = _invoke(cli, ["destroy", "demo", "--yes"])
    finally:
        cli.close()

    assert created_code == 0
    assert exit_code == 0
    assert "uploads versioned-bucket" in lines
    assert _option_line("versioning", True) not in lines

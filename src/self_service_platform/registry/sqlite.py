import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urlparse, urlunparse

from self_service_platform.core.manifest import ManifestEntry
from self_service_platform.core.registry import Clock
from self_service_platform.core.tenant import (
    DuplicateTenantError,
    Initiator,
    InitiatorKind,
    RegistryUnavailableError,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
    TransitionRejectedError,
    assert_allowed_transition,
    truncate_error,
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    tenant_type TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    initiator_kind TEXT NOT NULL,
    initiator_owner TEXT NOT NULL,
    expires_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
)
"""

_INSERT_TENANT = """
INSERT INTO tenants (
    tenant_id,
    tenant_type,
    owner,
    status,
    manifest_json,
    catalog_version,
    initiator_kind,
    initiator_owner,
    expires_at,
    last_error,
    updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_TRANSITION = """
UPDATE tenants
SET status = ?,
    manifest_json = ?,
    catalog_version = ?,
    initiator_kind = ?,
    initiator_owner = ?,
    expires_at = ?,
    last_error = ?,
    updated_at = ?
WHERE tenant_id = ? AND status = ?
"""

_UPDATE_FAILURE = """
UPDATE tenants
SET status = ?,
    initiator_kind = ?,
    initiator_owner = ?,
    expires_at = ?,
    last_error = ?,
    updated_at = ?
WHERE tenant_id = ? AND status = ?
"""

_SELECT_TENANT = """
SELECT
    tenant_id,
    tenant_type,
    owner,
    status,
    manifest_json,
    catalog_version,
    initiator_kind,
    initiator_owner,
    expires_at,
    last_error,
    updated_at
FROM tenants
WHERE tenant_id = ?
"""

_SELECT_TENANT_EXISTS = "SELECT 1 FROM tenants WHERE tenant_id = ?"

_SELECT_TENANTS = """
SELECT
    tenant_id,
    tenant_type,
    owner,
    status,
    manifest_json,
    catalog_version,
    initiator_kind,
    initiator_owner,
    expires_at,
    last_error,
    updated_at
FROM tenants
ORDER BY tenant_id
"""


class SqliteRegistry:
    def __init__(self, path: Path, clock: Clock, *, read_only: bool = False) -> None:
        self._path = path
        self._clock = clock
        if read_only:
            self._connection = _open_read_only_connection(path)
            return
        self._connection = sqlite3.connect(path, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(_CREATE_TABLE)
        self._connection.commit()

    @classmethod
    def open_read_only(cls, path: Path) -> Self:
        return cls(path, _read_clock, read_only=True)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def create_tenant(self, record: TenantRecord) -> None:
        if record.status is not TenantStatus.CREATING:
            raise TransitionRejectedError(
                f"create requires status creating, got {record.status.value}"
            )
        connection = self._connection
        try:
            connection.execute(
                _INSERT_TENANT,
                (
                    record.tenant_id,
                    record.tenant_type.value,
                    record.owner,
                    record.status.value,
                    _dump_manifest(record.manifest),
                    record.catalog_version,
                    record.initiator.kind.value,
                    record.initiator.owner,
                    _format_time(record.expires_at),
                    _format_error(record.last_error),
                    self._clock().isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise DuplicateTenantError(
                f"tenant already exists: {record.tenant_id}"
            ) from exc
        connection.commit()

    def get(self, tenant_id: str) -> TenantRecord:
        try:
            row = self._connection.execute(_SELECT_TENANT, (tenant_id,)).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise RegistryUnavailableError(self._path) from exc
        if row is None:
            raise TenantNotFoundError(f"no tenant {tenant_id}")
        return _record_from_row(row)

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        try:
            rows = self._connection.execute(_SELECT_TENANTS).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise RegistryUnavailableError(self._path) from exc
        return tuple(_record_from_row(row) for row in rows)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        assert_allowed_transition(expected, new)
        connection = self._connection
        statement = _transition_statement(
            tenant_id,
            expected,
            new,
            fields,
            self._clock,
        )
        cursor = connection.execute(statement[0], statement[1])
        if cursor.rowcount == 0:
            connection.rollback()
            _raise_for_missed_update(connection, tenant_id)
        connection.commit()
        return self.get(tenant_id)


def _read_clock() -> datetime:
    return datetime.now(UTC)


def _read_only_uri(path: Path) -> str:
    parsed = urlparse(path.resolve().as_uri())
    return urlunparse(parsed._replace(query="mode=ro"))


def _open_read_only_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(_read_only_uri(path), uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except (sqlite3.Error, OSError) as exc:
        raise RegistryUnavailableError(path) from exc
    return connection


def _transition_statement(
    tenant_id: str,
    expected: TenantStatus,
    new: TenantStatus,
    fields: TransitionFields,
    clock: Clock,
) -> tuple[str, tuple[object, ...]]:
    updated_at = clock().isoformat()
    if new in (TenantStatus.UPDATE_FAILED, TenantStatus.DESTROY_FAILED):
        return (
            _UPDATE_FAILURE,
            (
                new.value,
                fields.initiator.kind.value,
                fields.initiator.owner,
                _format_time(fields.expires_at),
                _format_error(fields.last_error),
                updated_at,
                tenant_id,
                expected.value,
            ),
        )
    last_error = None
    if not _clears_last_error(expected, new):
        last_error = _format_error(fields.last_error)
    return (
        _UPDATE_TRANSITION,
        (
            new.value,
            _dump_manifest(fields.manifest),
            fields.catalog_version,
            fields.initiator.kind.value,
            fields.initiator.owner,
            _format_time(fields.expires_at),
            last_error,
            updated_at,
            tenant_id,
            expected.value,
        ),
    )


def _clears_last_error(expected: TenantStatus, new: TenantStatus) -> bool:
    if expected is TenantStatus.UPDATING and new is TenantStatus.ACTIVE:
        return True
    return expected is TenantStatus.DESTROYING and new is TenantStatus.DESTROYED


def _raise_for_missed_update(connection: sqlite3.Connection, tenant_id: str) -> None:
    row = connection.execute(_SELECT_TENANT_EXISTS, (tenant_id,)).fetchone()
    if row is None:
        raise TenantNotFoundError(f"no tenant {tenant_id}")
    raise TransitionRejectedError(
        f"status of {tenant_id} did not match the expected status"
    )


def _dump_manifest(manifest: tuple[ManifestEntry, ...]) -> str:
    payload = [entry.model_dump(mode="json") for entry in manifest]
    return json.dumps(payload)


def _load_manifest(text: str) -> tuple[ManifestEntry, ...]:
    loaded = json.loads(text)
    return tuple(ManifestEntry.model_validate(item) for item in loaded)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _format_error(value: str | None) -> str | None:
    if value is None:
        return None
    return truncate_error(value)


def _record_from_row(row: sqlite3.Row) -> TenantRecord:
    expires_at = row["expires_at"]
    return TenantRecord(
        tenant_id=row["tenant_id"],
        tenant_type=TenantType(row["tenant_type"]),
        owner=row["owner"],
        status=TenantStatus(row["status"]),
        manifest=_load_manifest(row["manifest_json"]),
        catalog_version=row["catalog_version"],
        initiator=Initiator(
            kind=InitiatorKind(row["initiator_kind"]),
            owner=row["initiator_owner"],
        ),
        expires_at=None if expires_at is None else datetime.fromisoformat(expires_at),
        last_error=row["last_error"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from self_service_platform.catalog.component import AwsService
from self_service_platform.core.executor import (
    ExecutorError,
    ProvisionRequest,
    ResourceRef,
)
from self_service_platform.core.tags import build_tags
from self_service_platform.core.validator import ValidatedEntry
from self_service_platform.statedir import create_state_directory

_S3 = AwsService.S3.value
_BUSY_TIMEOUT_SECONDS = 5.0
_INJECTED_FAILURE = "injected provision failure"

_CREATE_RESOURCES = """
CREATE TABLE IF NOT EXISTS resources (
    tenant_id TEXT NOT NULL,
    component TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    tag_tenant_id TEXT NOT NULL,
    tag_tenant_type TEXT NOT NULL,
    tag_owner TEXT NOT NULL,
    tag_managed_by TEXT NOT NULL,
    tag_component TEXT NOT NULL,
    PRIMARY KEY (tenant_id, component)
)
"""

_CREATE_OBJECT_KEYS = """
CREATE TABLE IF NOT EXISTS object_keys (
    tenant_id TEXT NOT NULL,
    component TEXT NOT NULL,
    object_key TEXT NOT NULL,
    PRIMARY KEY (tenant_id, component, object_key)
)
"""

_CREATE_ROLES = """
CREATE TABLE IF NOT EXISTS tenant_roles (
    tenant_id TEXT PRIMARY KEY
)
"""

_SELECT_RESOURCES = """
SELECT
    resource_type,
    identifier,
    tag_tenant_id,
    tag_tenant_type,
    tag_owner,
    tag_managed_by,
    tag_component
FROM resources
WHERE tenant_id = ?
ORDER BY identifier
"""

_SELECT_RESOURCES_FOR_COMPONENT = """
SELECT
    resource_type,
    identifier,
    tag_tenant_id,
    tag_tenant_type,
    tag_owner,
    tag_managed_by,
    tag_component
FROM resources
WHERE tenant_id = ? AND tag_component = ?
ORDER BY identifier
"""

_SELECT_ROLE = "SELECT 1 FROM tenant_roles WHERE tenant_id = ?"

_SELECT_COMPONENTS = """
SELECT component, resource_type, identifier
FROM resources
WHERE tenant_id = ?
ORDER BY component
"""

_SELECT_RESOURCE_TYPE = """
SELECT resource_type FROM resources WHERE tenant_id = ? AND component = ?
"""

_SELECT_RESOURCE = """
SELECT resource_type, identifier
FROM resources
WHERE tenant_id = ? AND component = ?
"""

_COUNT_KEYS = """
SELECT COUNT(*) FROM object_keys WHERE tenant_id = ? AND component = ?
"""

_DELETE_KEYS = "DELETE FROM object_keys WHERE tenant_id = ? AND component = ?"

_INSERT_KEY = """
INSERT OR IGNORE INTO object_keys (tenant_id, component, object_key)
VALUES (?, ?, ?)
"""

_DELETE_RESOURCE = "DELETE FROM resources WHERE tenant_id = ? AND component = ?"

_UPSERT_RESOURCE = """
INSERT INTO resources (
    tenant_id,
    component,
    resource_type,
    identifier,
    tag_tenant_id,
    tag_tenant_type,
    tag_owner,
    tag_managed_by,
    tag_component
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (tenant_id, component) DO UPDATE SET
    resource_type = excluded.resource_type,
    identifier = excluded.identifier,
    tag_tenant_id = excluded.tag_tenant_id,
    tag_tenant_type = excluded.tag_tenant_type,
    tag_owner = excluded.tag_owner,
    tag_managed_by = excluded.tag_managed_by,
    tag_component = excluded.tag_component
"""

_INSERT_ROLE = "INSERT INTO tenant_roles (tenant_id) VALUES (?) ON CONFLICT DO NOTHING"

_DELETE_ROLE = "DELETE FROM tenant_roles WHERE tenant_id = ?"


class LocalExecutor:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fail_after: int | None = None
        self._fail_atomically = False
        self._leave_one = False

    def fail_next_provision(self, *, after: int = 0) -> None:
        self._fail_after = after

    def fail_next_provision_atomically(self) -> None:
        self._fail_atomically = True

    def leave_one_resource_on_next_destroy(self) -> None:
        self._leave_one = True

    def put_bucket_object(self, tenant_id: str, component: str, key: str) -> None:
        self._change(
            lambda connection: self._put_key(connection, tenant_id, component, key)
        )

    def delete_component_resource(self, tenant_id: str, component: str) -> None:
        self._change(
            lambda connection: self._delete_component(
                connection,
                tenant_id,
                component,
                empty_bucket=False,
            )
        )

    def provision(self, request: ProvisionRequest) -> None:
        fail_after = self._fail_after
        self._fail_after = None
        atomic = self._fail_atomically
        self._fail_atomically = False
        connection = self._open_write()
        try:
            self._provision(connection, request, fail_after, atomic)
        finally:
            connection.close()

    def destroy(self, tenant_id: str) -> None:
        leave_one = self._leave_one
        self._leave_one = False
        if _is_absent(self._path):
            return
        connection = self._open_write()
        try:
            self._destroy(connection, tenant_id, leave_one)
        finally:
            connection.close()

    def list_tagged_resources(
        self,
        tenant_id: str,
        component: str | None = None,
    ) -> tuple[ResourceRef, ...]:
        if _is_absent(self._path):
            return ()
        connection = self._open_read()
        try:
            statement = _SELECT_RESOURCES
            parameters: tuple[str, ...] = (tenant_id,)
            if component is not None:
                statement = _SELECT_RESOURCES_FOR_COMPONENT
                parameters = (tenant_id, component)
            try:
                rows = connection.execute(statement, parameters).fetchall()
            except (sqlite3.Error, OSError) as exc:
                raise ExecutorError(str(exc)) from exc
        finally:
            connection.close()
        return tuple(_resource_ref(row) for row in rows)

    def tenant_role_exists(self, tenant_id: str) -> bool:
        if _is_absent(self._path):
            return False
        connection = self._open_read()
        try:
            try:
                row = connection.execute(_SELECT_ROLE, (tenant_id,)).fetchone()
            except (sqlite3.Error, OSError) as exc:
                raise ExecutorError(str(exc)) from exc
        finally:
            connection.close()
        return row is not None

    def _provision(
        self,
        connection: sqlite3.Connection,
        request: ProvisionRequest,
        fail_after: int | None,
        atomic: bool,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _create_schema(connection)
            if atomic:
                entries = request.manifest.entries
                if entries:
                    self._upsert(connection, request, entries[0])
                raise ExecutorError(_INJECTED_FAILURE)
            for index, entry in enumerate(request.manifest.entries):
                if fail_after is not None and index >= fail_after:
                    connection.commit()
                    raise ExecutorError(_INJECTED_FAILURE)
                self._upsert(connection, request, entry)
            if fail_after is not None:
                connection.commit()
                raise ExecutorError(_INJECTED_FAILURE)
            self._remove_absent(connection, request)
            connection.execute(_INSERT_ROLE, (request.tenant_id,))
            connection.commit()
        except ExecutorError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError) as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ExecutorError(str(exc)) from exc

    def _destroy(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        leave_one: bool,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _require_schema(connection)
            names = [
                str(row["component"])
                for row in connection.execute(_SELECT_COMPONENTS, (tenant_id,))
            ]
            if leave_one and names:
                names = names[:-1]
            for name in names:
                self._delete_component(connection, tenant_id, name, empty_bucket=True)
            connection.execute(_DELETE_ROLE, (tenant_id,))
            connection.commit()
        except ExecutorError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError) as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ExecutorError(str(exc)) from exc

    def _change(self, work: Callable[[sqlite3.Connection], None]) -> None:
        connection = self._open_existing()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_schema(connection)
                work(connection)
                connection.commit()
            except ExecutorError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except (sqlite3.Error, OSError) as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise ExecutorError(str(exc)) from exc
        finally:
            connection.close()

    def _put_key(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        component: str,
        key: str,
    ) -> None:
        resource_type = _resource_type(connection, tenant_id, component)
        if resource_type != _S3:
            raise ExecutorError(f"{component} is not an S3 bucket")
        connection.execute(_INSERT_KEY, (tenant_id, component, key))

    def _delete_component(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        component: str,
        *,
        empty_bucket: bool,
    ) -> None:
        row = connection.execute(_SELECT_RESOURCE, (tenant_id, component)).fetchone()
        if row is None:
            raise ExecutorError(f"no resource {component} for {tenant_id}")
        if empty_bucket and row["resource_type"] == _S3:
            connection.execute(_DELETE_KEYS, (tenant_id, component))
        if row["resource_type"] == _S3 and _key_count(connection, tenant_id, component):
            raise ExecutorError(f"bucket {row['identifier']} is not empty")
        connection.execute(_DELETE_RESOURCE, (tenant_id, component))

    def _upsert(
        self,
        connection: sqlite3.Connection,
        request: ProvisionRequest,
        entry: ValidatedEntry,
    ) -> None:
        resource_type = entry.component.aws_service.value
        current = connection.execute(
            _SELECT_RESOURCE_TYPE,
            (request.tenant_id, entry.name),
        ).fetchone()
        keep_keys = (
            current is not None
            and current["resource_type"] == _S3
            and resource_type == _S3
        )
        if not keep_keys:
            connection.execute(_DELETE_KEYS, (request.tenant_id, entry.name))
        tags = build_tags(request, entry.name)
        connection.execute(
            _UPSERT_RESOURCE,
            (
                request.tenant_id,
                entry.name,
                resource_type,
                f"{request.tenant_id}/{entry.name}",
                tags["tenant_id"],
                tags["tenant_type"],
                tags["owner"],
                tags["managed_by"],
                tags["component"],
            ),
        )

    def _remove_absent(
        self,
        connection: sqlite3.Connection,
        request: ProvisionRequest,
    ) -> None:
        desired = {entry.name for entry in request.manifest.entries}
        rows = connection.execute(_SELECT_COMPONENTS, (request.tenant_id,)).fetchall()
        for row in rows:
            if str(row["component"]) not in desired:
                self._delete_component(
                    connection,
                    request.tenant_id,
                    str(row["component"]),
                    empty_bucket=True,
                )

    def _open_write(self) -> sqlite3.Connection:
        try:
            _ensure_directory(self._path.parent)
            if not self._path.exists():
                self._path.touch()
                if os.name == "posix":
                    self._path.chmod(0o600)
            connection = sqlite3.connect(
                self._path,
                timeout=_BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except (sqlite3.Error, OSError) as exc:
            raise ExecutorError(str(exc)) from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _open_existing(self) -> sqlite3.Connection:
        if _is_absent(self._path):
            raise ExecutorError(f"no tenant state in {self._path}")
        return self._open_write()

    def _open_read(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                _read_only_uri(self._path),
                uri=True,
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
        except (sqlite3.Error, OSError) as exc:
            raise ExecutorError(str(exc)) from exc
        return connection


def _is_absent(path: Path) -> bool:
    return not path.is_file() or path.stat().st_size == 0


def _ensure_directory(directory: Path) -> None:
    create_state_directory(directory)


def _read_only_uri(path: Path) -> str:
    parsed = urlparse(path.resolve().as_uri())
    return urlunparse(parsed._replace(query="mode=ro"))


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_CREATE_RESOURCES)
    connection.execute(_CREATE_OBJECT_KEYS)
    connection.execute(_CREATE_ROLES)


def _require_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {str(row["name"]) for row in rows}
    if not {"resources", "object_keys", "tenant_roles"}.issubset(names):
        raise ExecutorError("executor tables are missing")


def _resource_type(
    connection: sqlite3.Connection,
    tenant_id: str,
    component: str,
) -> str:
    row = connection.execute(_SELECT_RESOURCE_TYPE, (tenant_id, component)).fetchone()
    if row is None:
        raise ExecutorError(f"no resource {component} for {tenant_id}")
    return str(row["resource_type"])


def _key_count(connection: sqlite3.Connection, tenant_id: str, component: str) -> int:
    row = connection.execute(_COUNT_KEYS, (tenant_id, component)).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _resource_ref(row: sqlite3.Row) -> ResourceRef:
    return ResourceRef(
        resource_type=str(row["resource_type"]),
        identifier=str(row["identifier"]),
        tags={
            "tenant_id": str(row["tag_tenant_id"]),
            "tenant_type": str(row["tag_tenant_type"]),
            "owner": str(row["tag_owner"]),
            "managed_by": str(row["tag_managed_by"]),
            "component": str(row["tag_component"]),
        },
    )

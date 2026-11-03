import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.core.registry import Clock, Registry
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.tenant import (
    RegistryUnavailableError,
    SspHomeError,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TransitionFields,
)
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.registry.sqlite import SqliteRegistry
from self_service_platform.statedir import create_state_directory

_DATABASE_NAME = "tenants.sqlite"
_EXECUTOR_DATABASE_NAME = "local-executor.sqlite"
# POC defaults. Not from the spec.
_DEV_TTL = timedelta(days=1)
_EPHEMERAL_TTL = timedelta(hours=3)
_T = TypeVar("_T")


def state_directory() -> Path:
    """Return the directory that holds tenant state.

    State is kept outside the project so it does not depend on the working
    directory and does not land in git. It is also kept out of Documents and
    OneDrive, because a sync client locks or corrupts a SQLite file.
    """
    configured = os.environ.get("SSP_HOME")
    if configured is None:
        return Path.home() / ".ssp"
    path = Path(configured)
    if not path.is_absolute():
        raise SspHomeError(configured)
    return path


def database_path() -> Path:
    return state_directory() / _DATABASE_NAME


def executor_path() -> Path:
    return state_directory() / _EXECUTOR_DATABASE_NAME


def build_executor() -> LocalExecutor:
    return LocalExecutor(executor_path())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_service() -> PlatformService:
    return PlatformService(
        catalog=build_catalog(),
        registry=_WritableRegistry(database_path(), _utc_now),
        executor=build_executor(),
        clock=_utc_now,
        ttl_policy=TtlPolicy(dev=_DEV_TTL, ephemeral=_EPHEMERAL_TTL),
    )


def open_registry() -> Registry:
    path = database_path()
    if path.exists() and not path.is_file():
        raise RegistryUnavailableError(path)
    if not path.is_file():
        return _MissingState(path)
    return SqliteRegistry.open_read_only(path)


class _WritableRegistry:
    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock

    def create_tenant(self, record: TenantRecord) -> None:
        self._call(lambda registry: registry.create_tenant(record))

    def get(self, tenant_id: str) -> TenantRecord:
        if not self._path.is_file():
            if self._path.exists():
                raise RegistryUnavailableError(self._path)
            raise TenantNotFoundError(f"no tenant {tenant_id}")
        return self._read(lambda registry: registry.get(tenant_id))

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        if not self._path.is_file():
            if self._path.exists():
                raise RegistryUnavailableError(self._path)
            return ()
        return self._read(lambda registry: registry.list_tenants())

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

    def _call(self, work: Callable[[SqliteRegistry], _T]) -> _T:
        registry = self._open()
        try:
            return work(registry)
        except sqlite3.OperationalError as exc:
            raise RegistryUnavailableError(self._path) from exc
        finally:
            registry.close()

    def _open(self) -> SqliteRegistry:
        _prepare_database(self._path)
        try:
            return SqliteRegistry(self._path, self._clock)
        except OSError as exc:
            raise RegistryUnavailableError(self._path) from exc
        except sqlite3.OperationalError as exc:
            raise RegistryUnavailableError(self._path) from exc

    def _read(self, work: Callable[[SqliteRegistry], _T]) -> _T:
        try:
            registry = SqliteRegistry.open_read_only(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise RegistryUnavailableError(self._path) from exc
        try:
            return work(registry)
        except sqlite3.Error as exc:
            raise RegistryUnavailableError(self._path) from exc
        finally:
            registry.close()


def _prepare_database(path: Path) -> None:
    if path.exists() and not path.is_file():
        raise RegistryUnavailableError(path)
    try:
        create_state_directory(path.parent)
    except RegistryUnavailableError as exc:
        raise RegistryUnavailableError(path) from exc


class _MissingState:
    def __init__(self, path: Path) -> None:
        self._path = path

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        return ()

    def get(self, tenant_id: str) -> TenantRecord:
        raise TenantNotFoundError(f"no tenant {tenant_id}")

    def create_tenant(self, record: TenantRecord) -> None:
        raise RegistryUnavailableError(self._path)

    def transition(
        self,
        tenant_id: str,
        expected: TenantStatus,
        new: TenantStatus,
        fields: TransitionFields,
    ) -> TenantRecord:
        raise RegistryUnavailableError(self._path)

    def close(self) -> None:
        return None

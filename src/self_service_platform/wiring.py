import os
from pathlib import Path

from self_service_platform.core.registry import Registry
from self_service_platform.core.tenant import (
    RegistryUnavailableError,
    SspHomeError,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TransitionFields,
)
from self_service_platform.registry.sqlite import SqliteRegistry

_DATABASE_NAME = "tenants.sqlite"


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


def open_registry() -> Registry:
    path = database_path()
    if path.exists() and not path.is_file():
        raise RegistryUnavailableError(path)
    if not path.is_file():
        return _MissingState(path)
    return SqliteRegistry.open_read_only(path)


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

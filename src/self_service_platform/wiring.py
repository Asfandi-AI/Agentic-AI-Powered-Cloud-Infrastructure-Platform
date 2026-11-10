import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar, cast

import boto3

from self_service_platform.agent.loop import run_chat
from self_service_platform.audit.cloudwatch import LogsClient, RealCloudWatchAuditLog
from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.core.audit import (
    AuditEntry,
    CompositeAuditLog,
    Outcome,
    ToolName,
)
from self_service_platform.core.audit_file import JsonLinesAuditLog
from self_service_platform.core.chat_host import ChatHost
from self_service_platform.core.confirmation_issuer import confirmation_pair
from self_service_platform.core.fake_model_client import FakeModelClient
from self_service_platform.core.model_client import ModelClient
from self_service_platform.core.registry import Clock, Registry
from self_service_platform.core.service import PlatformService, TtlPolicy
from self_service_platform.core.tenant import (
    Initiator,
    RegistryUnavailableError,
    SspHomeError,
    TenantNotFoundError,
    TenantRecord,
    TenantStatus,
    TransitionFields,
)
from self_service_platform.executors.local import LocalExecutor
from self_service_platform.model.bedrock import BedrockModelClient, BedrockRuntimeClient
from self_service_platform.registry.sqlite import SqliteRegistry
from self_service_platform.statedir import create_state_directory

_DATABASE_NAME = "tenants.sqlite"
_EXECUTOR_DATABASE_NAME = "local-executor.sqlite"
_LOG_STREAM = "ssp-audit-log"
_DEFAULT_BEDROCK_REGION = "us-east-1"
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
    clock = _utc_now
    issuer, verifier = confirmation_pair(clock)
    return PlatformService(
        catalog=build_catalog(),
        registry=_WritableRegistry(database_path(), clock),
        executor=build_executor(),
        clock=clock,
        ttl_policy=TtlPolicy(dev=_DEV_TTL, ephemeral=_EPHEMERAL_TTL),
        audit_log=_AuditAfterStateExists(state_directory()),
        issuer=issuer,
        verifier=verifier,
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


def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    return text


def build_model_client() -> FakeModelClient | BedrockModelClient:
    """Return Bedrock when SSP_BEDROCK_MODEL_ID is set, otherwise an empty fake."""
    model_id = _setting("SSP_BEDROCK_MODEL_ID")
    if model_id is None:
        return FakeModelClient(())
    region = _setting("SSP_BEDROCK_REGION")
    if region is None:
        region = _DEFAULT_BEDROCK_REGION
    runtime = cast(
        BedrockRuntimeClient,
        boto3.client("bedrock-runtime", region_name=region),
    )
    return BedrockModelClient(runtime, model_id=model_id, region=region)


def run_configured_chat(
    service: PlatformService,
    client: ModelClient,
    initiator: Initiator,
    host: ChatHost,
) -> None:
    """Run one chat session with the client chosen for this process."""
    run_chat(service, client, initiator, host)


def _audit_sink(directory: Path) -> JsonLinesAuditLog | CompositeAuditLog:
    file_sink = JsonLinesAuditLog(directory)
    region = _setting("SSP_AWS_REGION")
    group = _setting("SSP_LOG_GROUP")
    if region is None or group is None:
        return file_sink
    client = cast(LogsClient, boto3.client("logs", region_name=region))
    cloud = RealCloudWatchAuditLog(client, log_group=group, log_stream=_LOG_STREAM)
    return CompositeAuditLog((file_sink, cloud))


class _AuditAfterStateExists:
    """Writes audit entries when an execute or destroy succeeds.

    Entries stay in memory until that success. A plan, a decline, a refusal,
    or a failed attempt does not create SSP_HOME and does not change a state
    directory that already exists. The registry creates the directory before
    the success entry is recorded. When SSP_AWS_REGION and SSP_LOG_GROUP are
    set, the same entries also go to CloudWatch Logs.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._sink = _audit_sink(directory)
        self._pending: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        self._pending.append(entry)
        if not _succeeded_mutation(entry) or not self._directory.is_dir():
            return
        pending = self._pending
        self._pending = []
        for item in pending:
            self._sink.write(item)


def _succeeded_mutation(entry: AuditEntry) -> bool:
    return entry.outcome is Outcome.SUCCEEDED and entry.tool_name in {
        ToolName.EXECUTE_CREATE,
        ToolName.EXECUTE_UPDATE,
        ToolName.DESTROY,
    }


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

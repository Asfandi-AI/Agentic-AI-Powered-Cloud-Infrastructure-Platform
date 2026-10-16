from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from self_service_platform.core.manifest import ManifestEntry
from self_service_platform.core.tenant import (
    Initiator,
    InitiatorKind,
    TenantRecord,
    TenantStatus,
    TenantType,
    TransitionFields,
)

_NAIVE = datetime(2026, 10, 11, 12, 0)
_UTC = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _record(expires_at: datetime | None, updated_at: datetime) -> TenantRecord:
    return TenantRecord(
        tenant_id="tenant-a",
        tenant_type=TenantType.DEV,
        status=TenantStatus.CREATING,
        manifest=(ManifestEntry(type="s3-bucket", name="uploads", options={}),),
        catalog_version="catalog-1",
        owner="alice",
        initiator=Initiator(kind=InitiatorKind.CLI, owner="alice"),
        expires_at=expires_at,
        last_error=None,
        updated_at=updated_at,
    )


def _fields(expires_at: datetime | None) -> TransitionFields:
    return TransitionFields(
        manifest=(ManifestEntry(type="s3-bucket", name="uploads", options={}),),
        catalog_version="catalog-1",
        initiator=Initiator(kind=InitiatorKind.CLI, owner="alice"),
        expires_at=expires_at,
        last_error=None,
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: _record(expires_at=_NAIVE, updated_at=_UTC),
            id="record-expires-at",
        ),
        pytest.param(
            lambda: _record(expires_at=_UTC, updated_at=_NAIVE),
            id="record-updated-at",
        ),
        pytest.param(
            lambda: _fields(expires_at=_NAIVE),
            id="transition-expires-at",
        ),
    ],
)
def test_naive_datetime_is_rejected(build: Callable[[], object]) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        build()


def test_transition_fields_have_no_owner_field() -> None:
    assert set(TransitionFields.model_fields) == {
        "catalog_version",
        "expires_at",
        "initiator",
        "last_error",
        "manifest",
    }


@pytest.mark.parametrize("owner", ["", " ", " \t\n"])
def test_tenant_record_rejects_a_blank_owner(owner: str) -> None:
    with pytest.raises(ValidationError):
        TenantRecord(
            tenant_id="tenant-a",
            tenant_type=TenantType.DEV,
            status=TenantStatus.CREATING,
            manifest=(ManifestEntry(type="s3-bucket", name="uploads", options={}),),
            catalog_version="catalog-1",
            owner=owner,
            initiator=Initiator(kind=InitiatorKind.CLI, owner="alice"),
            expires_at=_UTC,
            last_error=None,
            updated_at=_UTC,
        )

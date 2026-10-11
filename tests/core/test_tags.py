from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.core.executor import ProvisionRequest
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.tags import build_tags
from self_service_platform.core.tenant import TenantType
from self_service_platform.core.validator import validate_manifest


def test_build_tags_produces_every_required_catalog_tag() -> None:
    catalog = build_catalog()
    manifest = validate_manifest(
        Manifest.model_validate([{"type": "s3-bucket", "name": "uploads"}]),
        catalog,
    )
    request = ProvisionRequest(
        tenant_id="tenant-a",
        tenant_type=TenantType.DEV,
        owner="alice",
        manifest=manifest,
    )
    tags = build_tags(request, "uploads")

    for component in catalog.list_components():
        assert component.required_tags <= set(tags)

from collections.abc import Callable

import pytest

from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.core.executor import Executor, ProvisionRequest
from self_service_platform.core.manifest import Manifest
from self_service_platform.core.tags import build_tags
from self_service_platform.core.tenant import TenantType
from self_service_platform.core.validator import ValidatedManifest, validate_manifest
from self_service_platform.executors.fake import FakeExecutor

EXECUTOR_FACTORIES = [pytest.param(FakeExecutor, id="fake")]


def _manifest(*entries: tuple[str, str]) -> ValidatedManifest:
    raw = Manifest.model_validate(
        [{"type": component_type, "name": name} for component_type, name in entries]
    )
    return validate_manifest(raw, build_catalog())


def _request(
    manifest: ValidatedManifest,
    *,
    tenant_id: str = "tenant-a",
    owner: str = "alice",
) -> ProvisionRequest:
    return ProvisionRequest(
        tenant_id=tenant_id,
        tenant_type=TenantType.DEV,
        owner=owner,
        manifest=manifest,
    )


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_every_resource_gets_all_five_tag_values(
    factory: Callable[[], Executor],
) -> None:
    manifest = _manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs"))
    request = _request(manifest)
    executor = factory()
    executor.provision(request)

    resources = executor.list_tagged_resources(request.tenant_id)

    assert len(resources) == len(manifest.entries)
    for entry in manifest.entries:
        expected = build_tags(request, entry.name)
        matches = [
            resource
            for resource in resources
            if resource.tags["component"] == entry.name
        ]
        assert len(matches) == 1
        resource = matches[0]
        assert resource.resource_type == entry.component.aws_service.value
        assert dict(resource.tags) == dict(expected)
        assert resource.tags["tenant_id"] == request.tenant_id
        assert resource.tags["tenant_type"] == request.tenant_type.value
        assert resource.tags["owner"] == request.owner
        assert resource.tags["managed_by"] == expected["managed_by"]
        assert resource.tags["component"] == entry.name


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_provisioning_twice_leaves_identical_state(
    factory: Callable[[], Executor],
) -> None:
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = factory()
    executor.provision(request)
    first = executor.list_tagged_resources(request.tenant_id)
    role_after_first = executor.tenant_role_exists(request.tenant_id)

    executor.provision(request)

    assert executor.list_tagged_resources(request.tenant_id) == first
    assert executor.tenant_role_exists(request.tenant_id) is role_after_first


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_provision_deletes_only_the_removed_component(
    factory: Callable[[], Executor],
) -> None:
    executor = factory()
    tenant_id = "tenant-a"
    executor.provision(
        _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    )
    kept = executor.list_tagged_resources(tenant_id, component="uploads")

    executor.provision(_request(_manifest(("s3-bucket", "uploads"))))

    assert executor.list_tagged_resources(tenant_id, component="jobs") == ()
    assert executor.list_tagged_resources(tenant_id, component="uploads") == kept


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_destroy_leaves_no_tagged_resources_and_no_tenant_role(
    factory: Callable[[], Executor],
) -> None:
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = factory()
    executor.provision(request)

    executor.destroy(request.tenant_id)

    assert executor.list_tagged_resources(request.tenant_id) == ()
    assert executor.tenant_role_exists(request.tenant_id) is False


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_destroy_twice_succeeds(factory: Callable[[], Executor]) -> None:
    request = _request(_manifest(("sqs-queue", "jobs")))
    executor = factory()
    executor.provision(request)

    executor.destroy(request.tenant_id)
    executor.destroy(request.tenant_id)
    executor.destroy("never-created")


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_tenants_are_isolated(factory: Callable[[], Executor]) -> None:
    manifest = _manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs"))
    executor = factory()
    executor.provision(_request(manifest, tenant_id="tenant-a", owner="alice"))
    executor.provision(_request(manifest, tenant_id="tenant-b", owner="bob"))

    executor.destroy("tenant-a")

    assert executor.list_tagged_resources("tenant-a") == ()
    assert executor.tenant_role_exists("tenant-a") is False
    remaining = executor.list_tagged_resources("tenant-b")
    assert {resource.tags["tenant_id"] for resource in remaining} == {"tenant-b"}
    assert {resource.tags["component"] for resource in remaining} == {"uploads", "jobs"}
    assert executor.tenant_role_exists("tenant-b") is True


@pytest.mark.parametrize("factory", EXECUTOR_FACTORIES)
def test_tenant_role_exists_after_provision_and_not_after_destroy(
    factory: Callable[[], Executor],
) -> None:
    request = _request(_manifest(("s3-bucket", "uploads")))
    executor = factory()

    executor.provision(request)
    assert executor.tenant_role_exists(request.tenant_id) is True

    executor.destroy(request.tenant_id)
    assert executor.tenant_role_exists(request.tenant_id) is False

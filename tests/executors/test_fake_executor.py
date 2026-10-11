import pytest

from self_service_platform.core.executor import ExecutorError
from self_service_platform.executors.fake import FakeExecutor
from tests.executors.test_executor_contract import _manifest, _request


def test_injected_provision_failure_raises_and_leaves_partial_resources() -> None:
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = FakeExecutor()
    executor.fail_next_provision(after=1)

    with pytest.raises(ExecutorError):
        executor.provision(request)

    resources = executor.list_tagged_resources(request.tenant_id)
    assert len(resources) == 1
    assert resources[0].tags["component"] == "uploads"
    assert executor.tenant_role_exists(request.tenant_id) is False


def test_injected_destroy_leaves_one_resource() -> None:
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = FakeExecutor()
    executor.provision(request)
    executor.leave_one_resource_on_next_destroy()

    executor.destroy(request.tenant_id)

    remaining = executor.list_tagged_resources(request.tenant_id)
    assert len(remaining) == 1
    assert executor.tenant_role_exists(request.tenant_id) is False


def test_non_empty_bucket_delete_raises_executor_error() -> None:
    request = _request(_manifest(("s3-bucket", "uploads")))
    executor = FakeExecutor()
    executor.provision(request)
    executor.put_bucket_object(request.tenant_id, "uploads", "notes.txt")

    with pytest.raises(ExecutorError):
        executor.delete_component_resource(request.tenant_id, "uploads")

    assert len(executor.list_tagged_resources(request.tenant_id)) == 1


def test_destroy_empties_s3_buckets_before_deleting_them() -> None:
    request = _request(_manifest(("s3-bucket", "uploads"), ("sqs-queue", "jobs")))
    executor = FakeExecutor()
    executor.provision(request)
    executor.put_bucket_object(request.tenant_id, "uploads", "notes.txt")

    executor.destroy(request.tenant_id)

    assert executor.list_tagged_resources(request.tenant_id) == ()
    assert executor.tenant_role_exists(request.tenant_id) is False

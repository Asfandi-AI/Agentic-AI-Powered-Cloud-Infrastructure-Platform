"""In-memory stand-in for a CloudWatch Logs sink. This is not CloudWatch."""

from self_service_platform.core.audit import AuditEntry


class FakeCloudWatchLogs:
    """Records entries in order. It does not call AWS."""

    def __init__(self, log_group: str) -> None:
        self.log_group = log_group
        self.entries: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

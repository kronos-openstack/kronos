"""Tests for the migration-result notification endpoint."""

from __future__ import annotations

from kronos.engine.cooldown import QUARANTINE_FOREVER, CooldownTracker
from kronos.engine.result_listener import MigrationResultEndpoint


def _endpoint(
    max_retries: int = 3,
    quarantine_seconds: int = 3600,
) -> tuple[MigrationResultEndpoint, CooldownTracker]:
    tracker = CooldownTracker(
        aggregate_cooldown_seconds=600.0,
        instance_cooldown_seconds=900.0,
    )
    endpoint = MigrationResultEndpoint(
        cooldown=tracker,
        max_retries=max_retries,
        quarantine_seconds=quarantine_seconds,
    )
    return endpoint, tracker


def _fail_payload(
    retry_count: int,
    error_type: str,
    instance_uuid: str = "vm-1",
    aggregate: str = "gpu",
) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "aggregate": aggregate,
        "instance_uuid": instance_uuid,
        "from_host": "host-a",
        "to_host": "host-b",
        "success": False,
        "error": "boom",
        "error_type": error_type,
        "retry_count": retry_count,
    }


def _success_payload(instance_uuid: str = "vm-1") -> dict[str, object]:
    return {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "aggregate": "gpu",
        "instance_uuid": instance_uuid,
        "from_host": "host-a",
        "to_host": "host-b",
        "success": True,
        "error": "",
        "error_type": "",
        "retry_count": 0,
    }


class TestCompleted:
    def test_completed_never_quarantines(self) -> None:
        endpoint, tracker = _endpoint()
        endpoint.info({}, "pub", "migration.completed", _success_payload(), {})
        assert not tracker.is_instance_quarantined("vm-1")
        assert not tracker.is_instance_cooling("vm-1")


class TestFailedRetryPending:
    def test_retry_pending_no_state_change(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=1, error_type="MigrationFailed")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert not tracker.is_instance_quarantined("vm-1")
        assert not tracker.is_instance_cooling("vm-1")

    def test_first_attempt_is_retry_pending(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=0, error_type="PreFlightError")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert not tracker.is_instance_quarantined("vm-1")


class TestFailedFinal:
    def test_preflight_final_quarantines(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3, quarantine_seconds=1800)
        payload = _fail_payload(retry_count=3, error_type="PreFlightError")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert tracker.is_instance_quarantined("vm-1")

    def test_migration_failed_final_quarantines(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=3, error_type="MigrationFailed")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert tracker.is_instance_quarantined("vm-1")

    def test_timeout_final_quarantines(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=3, error_type="MigrationTimeout")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert tracker.is_instance_quarantined("vm-1")

    def test_nova_client_error_no_quarantine(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=3, error_type="NovaClientError")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert not tracker.is_instance_quarantined("vm-1")

    def test_unknown_error_type_quarantines_defensively(self) -> None:
        endpoint, tracker = _endpoint(max_retries=3)
        payload = _fail_payload(retry_count=3, error_type="SomethingUnknown")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert tracker.is_instance_quarantined("vm-1")

    def test_forever_quarantine_honoured(self) -> None:
        endpoint, tracker = _endpoint(
            max_retries=3, quarantine_seconds=QUARANTINE_FOREVER,
        )
        payload = _fail_payload(retry_count=3, error_type="MigrationFailed")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        import math
        assert tracker._instance_quarantine["vm-1"] == math.inf

    def test_max_retries_zero_means_first_is_final(self) -> None:
        endpoint, tracker = _endpoint(max_retries=0)
        payload = _fail_payload(retry_count=0, error_type="MigrationFailed")
        endpoint.info({}, "pub", "migration.failed", payload, {})
        assert tracker.is_instance_quarantined("vm-1")


class TestUnknownEvent:
    def test_unknown_event_is_ignored(self) -> None:
        endpoint, tracker = _endpoint()
        payload = _fail_payload(retry_count=3, error_type="MigrationFailed")
        endpoint.info({}, "pub", "migration.unknown", payload, {})
        assert not tracker.is_instance_quarantined("vm-1")

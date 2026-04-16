"""Tests for the migration runner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kronos.clients.nova import MigrationStatus
from kronos.common.exceptions import NovaClientError
from kronos.engine.types import MigrationPhase, MigrationTask
from kronos.executor.migrate import MigrationRunner


def _make_task(**overrides: object) -> MigrationTask:
    defaults: dict[str, object] = {
        "task_id": "task-123",
        "plan_id": "plan-456",
        "aggregate": "test-agg",
        "policy_names": ["cpu-spread"],
        "instance_uuid": "uuid-abc",
        "instance_name": "vm-1",
        "from_host": "host-a",
        "to_host": "host-b",
        "improvement": 0.1,
        "phase": MigrationPhase.SPREAD,
    }
    defaults.update(overrides)
    return MigrationTask(**defaults)


@pytest.fixture()
def runner() -> tuple[MigrationRunner, MagicMock]:
    conf = MagicMock()
    conf.executor.migration_timeout = 60
    conf.executor.migration_poll_interval = 1
    nova = MagicMock()
    r = MigrationRunner(conf, nova)
    return r, nova


class TestPreFlight:
    def test_success(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"

        # Should not raise
        r._pre_flight(_make_task())

    def test_not_active(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("SHUTOFF", None)

        result = r.execute(_make_task())
        assert not result.success
        assert "SHUTOFF" in result.error

    def test_pending_task_state(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", "migrating")

        result = r.execute(_make_task())
        assert not result.success
        assert "migrating" in result.error

    def test_wrong_host(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-c"

        result = r.execute(_make_task())
        assert not result.success
        assert "host-c" in result.error


class TestExecuteSuccess:
    def test_full_lifecycle(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        # Pre-flight
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ACTIVE", None),   # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # pre-flight
            "host-b",  # post-flight
        ]
        # Poll: migration gone (completed)
        nova.get_migration_status.return_value = None

        result = r.execute(_make_task())

        assert result.success
        assert result.duration_seconds > 0
        nova.live_migrate.assert_called_once_with("uuid-abc", "host-b")

    def test_polls_until_complete(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ACTIVE", None),   # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # pre-flight
            "host-b",  # post-flight
        ]
        # Poll: running, running, then gone
        nova.get_migration_status.side_effect = [
            MigrationStatus.RUNNING,
            MigrationStatus.RUNNING,
            None,
        ]

        with patch("kronos.executor.migrate.time.sleep"):
            result = r.execute(_make_task())

        assert result.success
        assert nova.get_migration_status.call_count == 3


class TestExecuteFailure:
    def test_migration_error_status(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.get_migration_status.return_value = MigrationStatus.ERROR

        result = r.execute(_make_task())

        assert not result.success
        assert "error" in result.error.lower()

    def test_migration_timeout(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        r._timeout = 0  # immediate timeout

        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.get_migration_status.return_value = MigrationStatus.RUNNING

        result = r.execute(_make_task())

        assert not result.success
        assert "timed out" in result.error.lower() or "timeout" in result.error.lower()

    def test_post_flight_wrong_host(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ACTIVE", None),   # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # pre-flight
            "host-a",  # post-flight — still on source!
        ]
        nova.get_migration_status.return_value = None

        result = r.execute(_make_task())

        assert not result.success
        assert "host-a" in result.error

    def test_post_flight_not_active(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ERROR", None),    # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # pre-flight
            "host-b",  # post-flight — right host but wrong status
        ]
        nova.get_migration_status.return_value = None

        result = r.execute(_make_task())

        assert not result.success
        assert "ERROR" in result.error

    def test_nova_api_error(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.live_migrate.side_effect = NovaClientError(reason="API 500")

        result = r.execute(_make_task())

        assert not result.success

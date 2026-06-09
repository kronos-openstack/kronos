"""Tests for the migration runner."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kronos.clients.nova import ComputeService, MigrationStatus
from kronos.common.exceptions import NovaClientError
from kronos.engine.types import MigrationPhase, MigrationTask
from kronos.executor.migrate import MigrationRunner


def _all_up_services(*hosts: str) -> list[ComputeService]:
    return [
        ComputeService(
            host=h, binary="nova-compute", state="up", status="enabled",
        )
        for h in hosts
    ]


def _make_task(**overrides: Any) -> MigrationTask:
    defaults: dict[str, Any] = {
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
    # Default: both source and destination are up + enabled.
    nova.list_compute_services.return_value = _all_up_services(
        "host-a", "host-b",
    )
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
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ACTIVE", None),   # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # _migration_still_needed
            "host-a",  # pre-flight
            "host-b",  # _poll_until_complete (status=None disambiguation)
            "host-b",  # post-flight
        ]
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
            "host-a",  # _migration_still_needed
            "host-a",  # pre-flight
            "host-b",  # _poll_until_complete (status=None disambiguation)
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

    def test_already_at_destination_is_noop(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Instance already on to_host short-circuits without calling live_migrate.

        Covers the post-flight race fix: the engine may dispatch a task that
        completed between planning and execution, or a duplicate after a
        retry; either way the work is done and re-issuing live_migrate would
        be a no-op or an error.
        """
        r, nova = runner
        nova.get_instance_host.return_value = "host-b"

        result = r.execute(_make_task())

        assert result.success
        nova.live_migrate.assert_not_called()
        nova.get_instance_status.assert_not_called()


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

    def test_migration_disappeared_without_arriving(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Active migration vanishes from Nova's list but instance is still on source.

        Distinguishes a true disappearance (Nova dropped the migration record
        after a failure) from the pending case (record not yet materialized
        immediately after live_migrate).  The poll loop only treats this as a
        failure once it has seen the migration in an active state at least once.
        """
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.side_effect = [
            "host-a",  # _migration_still_needed
            "host-a",  # pre-flight
            "host-a",  # _poll_until_complete (status=None, still on source)
        ]
        nova.get_migration_status.side_effect = [
            MigrationStatus.RUNNING,
            None,
        ]

        with patch("kronos.executor.migrate.time.sleep"):
            result = r.execute(_make_task())

        assert not result.success
        assert "disappeared" in result.error.lower()

    def test_post_flight_not_active(self, runner: tuple[MigrationRunner, MagicMock]) -> None:
        r, nova = runner
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),   # pre-flight
            ("ERROR", None),    # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # _migration_still_needed
            "host-a",  # pre-flight
            "host-b",  # _poll_until_complete (status=None disambiguation)
            "host-b",  # post-flight - right host but wrong status
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


class TestPreFlightServiceState:
    """Pre-flight re-checks nova-compute service state for src + dest."""

    def test_destination_disabled_blocks(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        # Source up, destination disabled between dispatch and execution.
        nova.list_compute_services.return_value = [
            ComputeService(
                host="host-a", binary="nova-compute",
                state="up", status="enabled",
            ),
            ComputeService(
                host="host-b", binary="nova-compute",
                state="up", status="disabled",
                disabled_reason="maintenance",
            ),
        ]

        result = r.execute(_make_task())

        assert not result.success
        assert "host-b" in result.error
        nova.live_migrate.assert_not_called()

    def test_destination_down_blocks(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.list_compute_services.return_value = [
            ComputeService(
                host="host-a", binary="nova-compute",
                state="up", status="enabled",
            ),
            ComputeService(
                host="host-b", binary="nova-compute",
                state="down", status="enabled",
            ),
        ]

        result = r.execute(_make_task())

        assert not result.success
        nova.live_migrate.assert_not_called()

    def test_destination_missing_blocks(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Nova doesn't list a service for the destination - refuse."""
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.list_compute_services.return_value = _all_up_services("host-a")

        result = r.execute(_make_task())

        assert not result.success
        nova.live_migrate.assert_not_called()

    def test_source_down_blocks(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Live migration cannot move a VM off a dead hypervisor."""
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.list_compute_services.return_value = [
            ComputeService(
                host="host-a", binary="nova-compute",
                state="down", status="enabled",
            ),
            ComputeService(
                host="host-b", binary="nova-compute",
                state="up", status="enabled",
            ),
        ]

        result = r.execute(_make_task())

        assert not result.success
        assert "host-a" in result.error
        nova.live_migrate.assert_not_called()

    def test_source_disabled_blocks_non_evacuation(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Spread/pack tasks refuse a source that became disabled."""
        r, nova = runner
        nova.get_instance_status.return_value = ("ACTIVE", None)
        nova.get_instance_host.return_value = "host-a"
        nova.list_compute_services.return_value = [
            ComputeService(
                host="host-a", binary="nova-compute",
                state="up", status="disabled",
                disabled_reason="late-disable",
            ),
            ComputeService(
                host="host-b", binary="nova-compute",
                state="up", status="enabled",
            ),
        ]

        result = r.execute(_make_task(phase=MigrationPhase.SPREAD))

        assert not result.success
        nova.live_migrate.assert_not_called()

    def test_evacuation_phase_allows_disabled_source(
        self, runner: tuple[MigrationRunner, MagicMock],
    ) -> None:
        """Evacuation tasks expect the source to be disabled - that's the point."""
        r, nova = runner
        nova.get_instance_status.side_effect = [
            ("ACTIVE", None),  # pre-flight
            ("ACTIVE", None),  # post-flight
        ]
        nova.get_instance_host.side_effect = [
            "host-a",  # _migration_still_needed
            "host-a",  # pre-flight
            "host-b",  # _poll_until_complete (status=None disambiguation)
            "host-b",  # post-flight
        ]
        nova.get_migration_status.return_value = None
        nova.list_compute_services.return_value = [
            ComputeService(
                host="host-a", binary="nova-compute",
                state="up", status="disabled",
                disabled_reason="maintenance",
            ),
            ComputeService(
                host="host-b", binary="nova-compute",
                state="up", status="enabled",
            ),
        ]

        result = r.execute(_make_task(phase=MigrationPhase.EVACUATE))

        assert result.success
        nova.live_migrate.assert_called_once_with("uuid-abc", "host-b")

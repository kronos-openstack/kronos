"""Tests for the multi-aggregate ExecutorWorker fan-out.

The worker builds one independent ``_AggregateExecutor`` per scope and
fans start/stop out to all of them.  These tests patch every external
dependency (transports, Nova client, runner, scheduler, RPC server) so
no broker is required - the focus is the supervisor wiring, not the
messaging layer.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from kronos.executor import worker as worker_mod
from kronos.executor.worker import ExecutorWorker


@pytest.fixture()
def patched() -> Iterator[dict[str, MagicMock]]:
    """Patch the worker's external collaborators with fresh mocks.

    ``get_rpc_server`` and ``TaskScheduler`` use side effects that return
    a new mock per call so each aggregate unit gets its own object and we
    can assert per-unit fan-out.
    """
    rpc_servers: list[MagicMock] = []
    schedulers: list[MagicMock] = []

    def new_server(*_args: object, **_kwargs: object) -> MagicMock:
        m = MagicMock(name=f"rpc_server_{len(rpc_servers)}")
        rpc_servers.append(m)
        return m

    def new_scheduler(*_args: object, **_kwargs: object) -> MagicMock:
        m = MagicMock(name=f"scheduler_{len(schedulers)}")
        schedulers.append(m)
        return m

    with (
        patch.object(worker_mod, "get_rpc_transport") as rpc_t,
        patch.object(worker_mod, "get_notification_transport") as notif_t,
        patch.object(worker_mod, "NovaClient") as nova,
        patch.object(worker_mod, "MigrationRunner") as runner,
        patch.object(worker_mod, "get_notifier") as notifier,
        patch.object(worker_mod, "get_rpc_client") as rpc_client,
        patch.object(worker_mod, "get_rpc_server", side_effect=new_server) as rpc_server,
        patch.object(worker_mod, "TaskScheduler", side_effect=new_scheduler) as scheduler,
    ):
        yield {
            "rpc_transport": rpc_t,
            "notification_transport": notif_t,
            "NovaClient": nova,
            "MigrationRunner": runner,
            "get_notifier": notifier,
            "get_rpc_client": rpc_client,
            "get_rpc_server": rpc_server,
            "TaskScheduler": scheduler,
            "rpc_servers": rpc_servers,
            "schedulers": schedulers,
        }


def _conf() -> MagicMock:
    conf = MagicMock()
    conf.executor.max_concurrent_migrations = 2
    conf.executor.retry_backoff = 1.0
    return conf


class TestExecutorWorkerFanOut:
    def test_one_unit_per_scope(self, patched: dict[str, MagicMock]) -> None:
        worker = ExecutorWorker(_conf(), ["gpu", "hpc", None])

        assert len(worker._aggregates) == 3
        assert [a.label for a in worker._aggregates] == ["gpu", "hpc", "<unassigned>"]
        # One Nova client, runner, scheduler, and RPC server per scope.
        assert patched["NovaClient"].call_count == 3
        assert patched["MigrationRunner"].call_count == 3
        assert patched["get_rpc_server"].call_count == 3
        assert patched["TaskScheduler"].call_count == 3
        # Transports are built once and shared across units.
        assert patched["rpc_transport"].call_count == 1
        assert patched["notification_transport"].call_count == 1

    def test_rpc_servers_bound_to_each_scope(
        self, patched: dict[str, MagicMock],
    ) -> None:
        ExecutorWorker(_conf(), ["gpu", "hpc", None])
        # get_rpc_server(transport, aggregate, [endpoint]) - 2nd positional
        # arg is the scope.
        scopes = [call.args[1] for call in patched["get_rpc_server"].call_args_list]
        assert scopes == ["gpu", "hpc", None]

    def test_start_fans_out(self, patched: dict[str, MagicMock]) -> None:
        worker = ExecutorWorker(_conf(), ["gpu", "hpc"])
        # Unblock the start() wait immediately so it returns after launch.
        worker._stop_event.set()
        worker.start()

        for server in patched["rpc_servers"]:
            server.start.assert_called_once()
        for sched in patched["schedulers"]:
            sched.start.assert_called_once()

    def test_stop_drains_every_unit(self, patched: dict[str, MagicMock]) -> None:
        worker = ExecutorWorker(_conf(), ["gpu", "hpc", None])
        worker.stop()

        for server in patched["rpc_servers"]:
            server.stop.assert_called_once()
            server.wait.assert_called_once()
        for sched in patched["schedulers"]:
            sched.stop.assert_called_once()

    def test_empty_scopes_rejected(self, patched: dict[str, MagicMock]) -> None:
        with pytest.raises(ValueError, match="at least one aggregate scope"):
            ExecutorWorker(_conf(), [])

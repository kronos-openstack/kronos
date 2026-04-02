"""Migration execution: pre-flight, Nova live-migrate, poll, post-flight.

The runner handles a single migration task from start to finish.
It never decides *what* to migrate — the engine's planner does that.
The runner only validates and executes.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.common.exceptions import (
    MigrationFailed,
    MigrationTimeout,
    NovaClientError,
    PreFlightError,
)
from kronos.engine.types import MigrationResult, MigrationTask

LOG = logging.getLogger(__name__)


class MigrationRunner:
    """Executes a single live migration against the Nova API.

    Lifecycle:
      1. Pre-flight check — instance is ACTIVE, idle, still on from_host
      2. Call Nova live-migrate
      3. Poll migration status until terminal or timeout
      4. Post-flight verify — instance is on to_host and ACTIVE
    """

    def __init__(self, conf: cfg.ConfigOpts, nova: NovaClient) -> None:
        self._nova = nova
        self._timeout = conf.executor.migration_timeout
        self._poll_interval = conf.executor.migration_poll_interval

    def execute(self, task: MigrationTask) -> MigrationResult:
        """Execute a migration task.

        :param task: The migration task to execute.
        :returns: MigrationResult with success/failure and duration.
        """
        started = time.monotonic()
        try:
            self._pre_flight(task)
            self._nova.live_migrate(task.instance_uuid, task.to_host)
            self._poll_until_complete(task)
            self._post_flight(task)

            duration = time.monotonic() - started
            LOG.info(
                "Migration %s completed in %.1fs: %s -> %s",
                task.task_id[:8],
                duration,
                task.from_host,
                task.to_host,
            )
            return MigrationResult(
                task_id=task.task_id,
                plan_id=task.plan_id,
                policy_name=task.policy_name,
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                success=True,
                duration_seconds=duration,
                retry_count=task.retry_count,
                completed_at=datetime.now(tz=UTC).isoformat(),
            )
        except (PreFlightError, MigrationFailed, MigrationTimeout, NovaClientError) as exc:
            duration = time.monotonic() - started
            LOG.error(
                "Migration %s failed after %.1fs: %s",
                task.task_id[:8],
                duration,
                exc,
            )
            return MigrationResult(
                task_id=task.task_id,
                plan_id=task.plan_id,
                policy_name=task.policy_name,
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                success=False,
                error=str(exc),
                duration_seconds=duration,
                retry_count=task.retry_count,
                completed_at=datetime.now(tz=UTC).isoformat(),
            )

    def _pre_flight(self, task: MigrationTask) -> None:
        """Validate the instance is in a migratable state."""
        status, task_state = self._nova.get_instance_status(task.instance_uuid)

        if status != "ACTIVE":
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=f"Instance status is {status}, expected ACTIVE.",
            )

        if task_state is not None:
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=f"Instance has pending task_state: {task_state}.",
            )

        current_host = self._nova.get_instance_host(task.instance_uuid)
        if current_host != task.from_host:
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=(
                    f"Instance is on {current_host}, "
                    f"expected {task.from_host}. It may have already moved."
                ),
            )

    def _poll_until_complete(self, task: MigrationTask) -> None:
        """Poll Nova until the migration reaches a terminal state or times out."""
        deadline = time.monotonic() + self._timeout

        while time.monotonic() < deadline:
            status = self._nova.get_migration_status(task.instance_uuid)

            if status is None:
                # No active migration — it either completed and disappeared,
                # or was never started. Check post-flight to determine.
                return

            if status.is_success:
                return

            if status.is_terminal:
                raise MigrationFailed(
                    instance_uuid=task.instance_uuid,
                    from_host=task.from_host,
                    to_host=task.to_host,
                    reason=f"Nova reports migration status: {status.value}",
                )

            LOG.debug(
                "Migration %s: status=%s, polling again in %ds",
                task.task_id[:8],
                status.value,
                self._poll_interval,
            )
            time.sleep(self._poll_interval)

        raise MigrationTimeout(
            instance_uuid=task.instance_uuid,
            timeout_seconds=self._timeout,
        )

    def _post_flight(self, task: MigrationTask) -> None:
        """Verify the instance actually arrived on the destination host."""
        current_host = self._nova.get_instance_host(task.instance_uuid)
        if current_host != task.to_host:
            raise MigrationFailed(
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                reason=(
                    f"Post-flight: instance is on {current_host}, "
                    f"expected {task.to_host}."
                ),
            )

        status, _ = self._nova.get_instance_status(task.instance_uuid)
        if status != "ACTIVE":
            raise MigrationFailed(
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                reason=f"Post-flight: instance status is {status}, expected ACTIVE.",
            )

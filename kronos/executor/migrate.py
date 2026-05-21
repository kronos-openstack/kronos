"""Migration execution: pre-flight, Nova live-migrate, poll, post-flight.

The runner handles a single migration task from start to finish.
It never decides *what* to migrate - the engine's planner does that.
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
    PlacementRejected,
    PreFlightError,
)
from kronos.engine.types import MigrationPhase, MigrationResult, MigrationTask

LOG = logging.getLogger(__name__)


class MigrationRunner:
    """Executes a single live migration against the Nova API.

    Lifecycle:
      0. No-op short-circuit if the instance is already on to_host
         (e.g. completed by an earlier dispatch, manual move out of band)
      1. Pre-flight check - instance is ACTIVE, idle, still on from_host
      2. Call Nova live-migrate
      3. Poll migration status until terminal or timeout
      4. Post-flight verify - instance is on to_host and ACTIVE
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
            if not self._migration_still_needed(task):
                duration = time.monotonic() - started
                LOG.info(
                    "Migration %s no-op: instance already on %s",
                    task.task_id[:8],
                    task.to_host,
                )
                return MigrationResult(
                    task_id=task.task_id,
                    plan_id=task.plan_id,
                    aggregate=task.aggregate,
                    instance_uuid=task.instance_uuid,
                    from_host=task.from_host,
                    to_host=task.to_host,
                    success=True,
                    duration_seconds=duration,
                    retry_count=task.retry_count,
                    completed_at=datetime.now(tz=UTC).isoformat(),
                )

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
                aggregate=task.aggregate,
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                success=True,
                duration_seconds=duration,
                retry_count=task.retry_count,
                completed_at=datetime.now(tz=UTC).isoformat(),
            )
        except (
            PreFlightError,
            MigrationFailed,
            MigrationTimeout,
            PlacementRejected,
            NovaClientError,
        ) as exc:
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
                aggregate=task.aggregate,
                instance_uuid=task.instance_uuid,
                from_host=task.from_host,
                to_host=task.to_host,
                success=False,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_seconds=duration,
                retry_count=task.retry_count,
                completed_at=datetime.now(tz=UTC).isoformat(),
            )

    def _migration_still_needed(self, task: MigrationTask) -> bool:
        """Return False if the instance is already on the destination host.

        The engine may dispatch a task that became a no-op between
        planning and execution: e.g. an earlier attempt's live-migrate
        completed after a post-flight race (Nova live-migrate is async),
        or an operator moved the VM out of band.  Either way the work
        is done; skip pre-flight and report success rather than retrying
        and quarantining a healthy instance.
        """
        return self._nova.get_instance_host(task.instance_uuid) != task.to_host

    def _pre_flight(self, task: MigrationTask) -> None:
        """Validate the instance and host are in a migratable state.

        Re-checks nova-compute service state for both source and
        destination right before live-migrate.  The engine already
        filtered destinations at plan time, but service state can flip
        between dispatch and execution (operator disables a host, a
        node loses heartbeat); refusing here closes that gap.

        The evacuation phase tolerates a ``status=disabled`` source -
        that's the whole point of evacuation, the source is *meant* to
        be drained.  All phases still require ``state=up`` on the
        source (live migration cannot move a VM off a dead hypervisor).
        """
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

        self._check_service_state(task)

    def _check_service_state(self, task: MigrationTask) -> None:
        """Refuse the move if source or destination service is unfit."""
        services = {
            svc.host: svc for svc in self._nova.list_compute_services()
        }

        source = services.get(task.from_host)
        if source is None or not source.is_up:
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=(
                    f"Source host '{task.from_host}' nova-compute service is "
                    f"{'missing' if source is None else 'down'}; "
                    "cannot live-migrate from a dead hypervisor."
                ),
            )

        is_evacuation = task.phase == MigrationPhase.EVACUATE
        if not is_evacuation and not source.is_enabled:
            # Non-evacuation phase plans expect the source to still be
            # in normal service.  A late-disabled source means an
            # operator just took it out of rotation; the executor
            # should not proceed - the engine can re-plan as an
            # evacuation if `evacuate_disabled_hosts` is on.
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=(
                    f"Source host '{task.from_host}' nova-compute service is "
                    f"administratively disabled ({source.disabled_reason!r}); "
                    "refusing non-evacuation migration."
                ),
            )

        dest = services.get(task.to_host)
        if dest is None or not dest.is_available_destination:
            raise PreFlightError(
                instance_uuid=task.instance_uuid,
                reason=(
                    f"Destination host '{task.to_host}' is not a valid "
                    f"live-migration target "
                    f"(state={dest.state if dest else 'missing'}, "
                    f"status={dest.status if dest else 'missing'}, "
                    f"forced_down={dest.forced_down if dest else 'n/a'})."
                ),
            )

    def _poll_until_complete(self, task: MigrationTask) -> None:
        """Poll Nova until the migration reaches a terminal state or times out.

        ``GET /servers/{id}/migrations`` only lists *active* migrations, so
        ``None`` is ambiguous: either the migration record has not been
        materialized yet (Nova's live-migrate API is async / 202), or it
        completed and was removed.  Disambiguate by checking the
        instance's current host.  A premature ``None`` immediately after
        ``live_migrate`` was previously read as completion, causing
        post-flight to race the conductor and fail.
        """
        deadline = time.monotonic() + self._timeout
        seen_active = False

        while time.monotonic() < deadline:
            status = self._nova.get_migration_status(task.instance_uuid)

            if status is None:
                if self._nova.get_instance_host(task.instance_uuid) == task.to_host:
                    return
                if seen_active:
                    raise MigrationFailed(
                        instance_uuid=task.instance_uuid,
                        from_host=task.from_host,
                        to_host=task.to_host,
                        reason=(
                            "Migration disappeared from Nova's active list "
                            "without arriving at destination."
                        ),
                    )
            elif status.is_success:
                return
            elif status.is_terminal:
                raise MigrationFailed(
                    instance_uuid=task.instance_uuid,
                    from_host=task.from_host,
                    to_host=task.to_host,
                    reason=f"Nova reports migration status: {status.value}",
                )
            else:
                seen_active = True
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

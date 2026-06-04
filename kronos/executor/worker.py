"""Executor worker: RPC server + notification publisher.

Receives ``execute_migration`` RPC casts from the engine on the per-aggregate
migrations topic, runs them via the scheduler, and publishes results as
notifications on the results topic so all engines (active and passive) can
update their cooldown state.

One process can service several aggregates at once: the worker builds one
independent :class:`_AggregateExecutor` per aggregate (or the unassigned
pool), each with its own scheduler, RPC server, Nova client, and migration
runner running on its own threads.  This is behaviour-identical to running
one single-aggregate executor process per aggregate; the only thing shared
across aggregates is the pair of oslo.messaging transports.
"""

from __future__ import annotations

import threading
import time

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.common.messaging import (
    UNASSIGNED_TOPIC_MARKER,
    dict_to_dataclass,
    get_notification_transport,
    get_notifier,
    get_rpc_client,
    get_rpc_server,
    get_rpc_transport,
    migrations_topic,
    results_topic,
    task_to_dict,
)
from kronos.engine.types import MigrationResult, MigrationTask
from kronos.executor.migrate import MigrationRunner
from kronos.executor.scheduler import TaskScheduler

LOG = logging.getLogger(__name__)

# Total wall-clock budget for draining every aggregate on shutdown.  The
# drain runs in daemon threads, so whatever is still in flight at the
# deadline dies with the process - Nova continues any migration it has
# already started regardless.
_SHUTDOWN_DEADLINE_SECONDS = 15.0


def _label(aggregate: str | None) -> str:
    """Human-readable scope label; ``None`` is the unassigned pool."""
    return aggregate if aggregate is not None else "<unassigned>"


class MigrationRPCEndpoint:
    """RPC endpoint: receives migration tasks from the engine.

    Exposes a single method, ``execute_migration``, invoked by the engine
    via ``RPCClient.cast()``.  The handler returns immediately after
    queuing the task with the scheduler - oslo.messaging acks the message.
    """

    target = oslo_messaging.Target(version="1.0")

    def __init__(self, scheduler: TaskScheduler) -> None:
        self._scheduler = scheduler

    def execute_migration(
        self,
        ctxt: dict[str, object],
        task: dict[str, object],
    ) -> None:
        """Handle an execute_migration RPC cast."""
        migration_task = dict_to_dataclass(MigrationTask, task)
        LOG.info(
            "Received migration task %s: %s (%s) %s -> %s",
            migration_task.task_id[:8],
            migration_task.instance_name,
            migration_task.instance_uuid[:8],
            migration_task.from_host,
            migration_task.to_host,
        )
        self._scheduler.submit(migration_task)


class _AggregateExecutor:
    """The per-aggregate execution unit.

    Wires together, for a single aggregate (or the unassigned pool):
      - RPC server: consumes migration tasks (engine -> executor)
      - Notifier: publishes migration results (executor -> engines)
      - Scheduler: respects ``not_before`` and concurrency limits
      - Migration runner: pre-flight, Nova live-migrate, post-flight
      - Retry client: re-casts failed tasks to the RPC topic

    The oslo.messaging transports are owned by the parent
    :class:`ExecutorWorker` and shared across sibling units; everything
    else here is private to this aggregate, so units never contend.
    """

    def __init__(
        self,
        conf: cfg.ConfigOpts,
        aggregate: str | None,
        rpc_transport: oslo_messaging.Transport,
        notification_transport: oslo_messaging.Transport,
    ) -> None:
        self._conf = conf
        # None means this unit services the unassigned-hosts pool.
        self._aggregate = aggregate
        self._label = _label(aggregate)

        self._nova = NovaClient(conf)
        self._runner = MigrationRunner(conf, self._nova)

        publisher_suffix = (
            aggregate if aggregate is not None else UNASSIGNED_TOPIC_MARKER
        )

        # Notifier for publishing results on the notification transport.
        self._result_notifier = get_notifier(
            notification_transport,
            results_topic(aggregate),
            publisher_id=f"kronos-executor-{publisher_suffix}",
        )

        # RPC client for re-casting retries back to the migrations topic.
        self._retry_rpc_client = get_rpc_client(rpc_transport, aggregate)

        # Scheduler: one concurrency budget per aggregate, matching the
        # behaviour of a dedicated single-aggregate executor process.
        self._scheduler = TaskScheduler(
            max_concurrent=conf.executor.max_concurrent_migrations,
            on_result=self._publish_result,
            run_task=self._execute_with_retry,
        )

        # RPC server (consumes engine casts for this aggregate's topic).
        endpoint = MigrationRPCEndpoint(self._scheduler)
        self._rpc_server = get_rpc_server(
            rpc_transport, aggregate, [endpoint],
        )

    @property
    def label(self) -> str:
        return self._label

    def start(self) -> None:
        """Start the scheduler and RPC server (both non-blocking)."""
        self._scheduler.start()
        self._rpc_server.start()
        LOG.info(
            "Executor ready for aggregate '%s'; RPC topic '%s'",
            self._label,
            migrations_topic(self._aggregate),
        )

    def drain(self) -> None:
        """Stop the RPC server and scheduler, best-effort.

        Exceptions are logged, not raised: shutdown must make progress
        across every sibling unit even if one transport misbehaves.
        """
        try:
            self._rpc_server.stop()
            self._rpc_server.wait()  # drain in-flight RPC handlers
        except Exception as exc:
            LOG.warning(
                "Error stopping RPC server for '%s': %s", self._label, exc,
            )
        try:
            self._scheduler.stop()
        except Exception as exc:
            LOG.warning(
                "Error stopping scheduler for '%s': %s", self._label, exc,
            )

    def _execute_with_retry(self, task: MigrationTask) -> MigrationResult:
        """Execute a task, re-casting to RPC on failure if retries remain."""
        result = self._runner.execute(task)

        if not result.success and task.retry_count < task.max_retries:
            self._retry(task)

        return result

    def _retry(self, task: MigrationTask) -> None:
        """Re-cast a failed task with incremented retry count and backoff."""
        backoff = self._conf.executor.retry_backoff * (2 ** task.retry_count)
        retry_task = MigrationTask(
            task_id=task.task_id,
            plan_id=task.plan_id,
            aggregate=task.aggregate,
            policy_names=list(task.policy_names),
            instance_uuid=task.instance_uuid,
            instance_name=task.instance_name,
            from_host=task.from_host,
            to_host=task.to_host,
            improvement=task.improvement,
            phase=task.phase,
            priority=task.priority,
            max_retries=task.max_retries,
            retry_count=task.retry_count + 1,
            not_before=task.not_before + backoff,
            created_at=task.created_at,
        )
        self._retry_rpc_client.cast(
            {}, "execute_migration", task=task_to_dict(retry_task),
        )
        LOG.info(
            "Re-cast migration %s for retry (attempt %d/%d, backoff=%.0fs)",
            task.task_id[:8],
            retry_task.retry_count,
            task.max_retries,
            backoff,
        )

    def _publish_result(self, result: MigrationResult) -> None:
        """Publish a migration result to the notifications results topic."""
        event_type = "migration.completed" if result.success else "migration.failed"
        self._result_notifier.info({}, event_type, task_to_dict(result))
        LOG.info(
            "Published %s for task %s (instance %s)",
            event_type,
            result.task_id[:8],
            result.instance_uuid[:8],
        )


class ExecutorWorker:
    """Top-level executor supervising one or more aggregates.

    Builds the two oslo.messaging transports once, then one independent
    :class:`_AggregateExecutor` per scope in ``scopes``.  A scope of
    ``None`` is the unassigned-hosts pool.  ``start()`` launches every
    unit and blocks until a stop is requested.
    """

    def __init__(
        self,
        conf: cfg.ConfigOpts,
        scopes: list[str | None],
    ) -> None:
        if not scopes:
            raise ValueError(
                "ExecutorWorker requires at least one aggregate scope",
            )
        self._conf = conf
        self._scopes = scopes

        # Two transports, shared across every aggregate unit: one for RPC
        # (engine <-> executor), one for notifications (executor -> engines).
        self._rpc_transport = get_rpc_transport(conf)
        self._notification_transport = get_notification_transport(conf)

        self._aggregates = [
            _AggregateExecutor(
                conf,
                scope,
                self._rpc_transport,
                self._notification_transport,
            )
            for scope in scopes
        ]
        self._labels = ", ".join(a.label for a in self._aggregates)

        # Blocks start() until stop() is called.
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start every aggregate unit. Blocks until stop is requested.

        Cleanup runs from this method (the main thread) once the stop
        event fires, not from the signal handler.  oslo.messaging
        ``stop()``/``wait()`` take locks that aren't safe to acquire
        from a signal-handler context, and a re-entrant signal during
        cleanup can deadlock.
        """
        LOG.info("Starting executor for aggregates: %s", self._labels)
        for aggregate in self._aggregates:
            aggregate.start()
        LOG.info(
            "Executor ready; servicing %d aggregate(s): %s",
            len(self._aggregates),
            self._labels,
        )
        # Block here - oslo.messaging RPC servers consume in background threads.
        self._stop_event.wait()
        self._shutdown()

    def request_stop(self) -> None:
        """Async-signal-safe stop request.  Call from signal handlers."""
        self._stop_event.set()

    def stop(self) -> None:
        """Synchronous stop: nudge the event and run cleanup inline.

        Suitable for tests and any caller already on the main thread.
        Signal handlers should call :meth:`request_stop` instead so
        that cleanup runs from the daemon's main thread.
        """
        self._stop_event.set()
        self._shutdown()

    def _shutdown(self) -> None:
        """Drain every aggregate unit in parallel with a hard-exit watchdog.

        ``MessageHandlingServer.wait()`` has no timeout in oslo.messaging;
        if a consumer thread doesn't unwind we run each unit's drain in
        its own daemon thread and give up after a shared deadline so the
        process actually exits.  Draining in parallel keeps a single
        stuck aggregate from eating the whole budget before its siblings
        get a chance to drain.
        """
        LOG.info("Stopping executor for aggregates: %s", self._labels)

        drainers = [
            threading.Thread(
                target=aggregate.drain,
                name=f"executor-shutdown-{aggregate.label}",
                daemon=True,
            )
            for aggregate in self._aggregates
        ]
        for drainer in drainers:
            drainer.start()

        deadline = time.monotonic() + _SHUTDOWN_DEADLINE_SECONDS
        for drainer in drainers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                drainer.join(timeout=remaining)

        if any(drainer.is_alive() for drainer in drainers):
            LOG.warning(
                "Executor shutdown did not complete within %.0fs; "
                "exiting anyway.",
                _SHUTDOWN_DEADLINE_SECONDS,
            )

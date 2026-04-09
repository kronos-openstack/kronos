"""Executor worker: RPC server + notification publisher.

Receives ``execute_migration`` RPC casts from the engine on the per-aggregate
migrations topic, runs them via the scheduler, and publishes results as
notifications on the results topic so all engines (active and passive) can
update their cooldown state.
"""

from __future__ import annotations

import threading

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


class MigrationRPCEndpoint:
    """RPC endpoint: receives migration tasks from the engine.

    Exposes a single method, ``execute_migration``, invoked by the engine
    via ``RPCClient.cast()``.  The handler returns immediately after
    queuing the task with the scheduler — oslo.messaging acks the message.
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


class ExecutorWorker:
    """Top-level executor for one aggregate.

    Wires together:
      - RPC server: consumes migration tasks (engine → executor)
      - Notifier: publishes migration results (executor → engines)
      - Scheduler: respects ``not_before`` and concurrency limits
      - Migration runner: pre-flight, Nova live-migrate, post-flight
      - Retry notifier: re-casts failed tasks to the RPC topic
    """

    def __init__(self, conf: cfg.ConfigOpts, aggregate: str | None) -> None:
        self._conf = conf
        # None means this executor services the unassigned-hosts pool
        self._aggregate = aggregate
        self._aggregate_label = (
            aggregate if aggregate is not None else "<unassigned>"
        )

        self._nova = NovaClient(conf)
        self._runner = MigrationRunner(conf, self._nova)

        # Two transports: one for RPC (engine ↔ executor),
        # one for notifications (executor → engines)
        self._rpc_transport = get_rpc_transport(conf)
        self._notification_transport = get_notification_transport(conf)

        publisher_suffix = (
            aggregate if aggregate is not None else UNASSIGNED_TOPIC_MARKER
        )

        # Notifier for publishing results on the notification transport
        self._result_notifier = get_notifier(
            self._notification_transport,
            results_topic(aggregate),
            publisher_id=f"kronos-executor-{publisher_suffix}",
        )

        # RPC client for re-casting retries back to the migrations topic
        self._retry_rpc_client = get_rpc_client(self._rpc_transport, aggregate)

        # Scheduler
        self._scheduler = TaskScheduler(
            max_concurrent=conf.executor.max_concurrent_migrations,
            on_result=self._publish_result,
            run_task=self._execute_with_retry,
        )

        # RPC server (consumes engine casts)
        endpoint = MigrationRPCEndpoint(self._scheduler)
        self._rpc_server = get_rpc_server(
            self._rpc_transport, aggregate, [endpoint],
        )

        # Blocks start() until stop() is called
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the executor. Blocks until stop() is called."""
        LOG.info(
            "Starting executor for aggregate '%s'",
            self._aggregate_label,
        )
        self._scheduler.start()
        self._rpc_server.start()
        LOG.info(
            "Executor ready; RPC topic '%s'",
            migrations_topic(self._aggregate),
        )
        # Block here — oslo.messaging RPC server consumes in background threads
        self._stop_event.wait()

    def stop(self) -> None:
        """Stop the executor gracefully."""
        LOG.info("Stopping executor for aggregate '%s'", self._aggregate_label)
        self._rpc_server.stop()
        self._rpc_server.wait()  # drain in-flight RPC calls
        self._scheduler.stop()
        self._stop_event.set()

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

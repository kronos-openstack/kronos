"""Executor worker: oslo.messaging notification endpoint.

Consumes ``migration.requested`` events from the per-aggregate topic,
hands tasks to the TaskScheduler, and publishes results back to the
results topic.
"""

from __future__ import annotations

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.common.messaging import (
    dict_to_dataclass,
    emit_notification,
    get_notifier,
    get_transport,
    migrations_topic,
    results_topic,
    task_to_dict,
)
from kronos.engine.types import MigrationResult, MigrationTask
from kronos.executor.migrate import MigrationRunner
from kronos.executor.scheduler import TaskScheduler

LOG = logging.getLogger(__name__)


class MigrationEndpoint:
    """oslo.messaging notification endpoint for migration tasks.

    Receives ``migration.requested`` events and submits them to the
    scheduler.  The handler returns immediately — oslo.messaging auto-acks.
    """

    def __init__(self, scheduler: TaskScheduler) -> None:
        self._scheduler = scheduler

    def info(
        self,
        ctxt: dict[str, object],
        publisher_id: str,
        event_type: str,
        payload: dict[str, object],
        metadata: dict[str, object],
    ) -> None:
        """Handle a migration.requested notification."""
        task = dict_to_dataclass(MigrationTask, payload)
        LOG.info(
            "Received migration task %s: %s (%s) %s -> %s",
            task.task_id[:8],
            task.instance_name,
            task.instance_uuid[:8],
            task.from_host,
            task.to_host,
        )
        self._scheduler.submit(task)


class ExecutorWorker:
    """Top-level executor that wires together messaging, scheduler, and runner.

    One ExecutorWorker per aggregate.  Start with ``.start()`` (blocks),
    stop with ``.stop()``.
    """

    def __init__(self, conf: cfg.ConfigOpts, aggregate: str) -> None:
        self._conf = conf
        self._aggregate = aggregate

        self._nova = NovaClient(conf)
        self._runner = MigrationRunner(conf, self._nova)
        self._transport = get_transport(conf)

        # Notifier for publishing results
        self._result_notifier = get_notifier(
            self._transport,
            results_topic(aggregate),
            publisher_id=f"kronos-executor-{aggregate}",
        )

        # Notifier for re-publishing retries to the migrations topic
        self._migration_notifier = get_notifier(
            self._transport,
            migrations_topic(aggregate),
            publisher_id=f"kronos-executor-{aggregate}",
        )

        # Scheduler
        self._scheduler = TaskScheduler(
            max_concurrent=conf.executor.max_concurrent_migrations,
            on_result=self._publish_result,
            run_task=self._execute_with_retry,
        )

        # Listener
        target = oslo_messaging.Target(
            topic=migrations_topic(aggregate),
        )
        endpoint = MigrationEndpoint(self._scheduler)
        self._listener = oslo_messaging.get_notification_listener(
            self._transport,
            [target],
            [endpoint],
            executor="threading",
        )

    def start(self) -> None:
        """Start the executor. Blocks until stopped."""
        LOG.info(
            "Starting executor for aggregate '%s'",
            self._aggregate,
        )
        self._scheduler.start()
        self._listener.start()
        self._listener.wait()

    def stop(self) -> None:
        """Stop the executor gracefully."""
        LOG.info("Stopping executor for aggregate '%s'", self._aggregate)
        self._listener.stop()
        self._scheduler.stop()

    def _execute_with_retry(self, task: MigrationTask) -> MigrationResult:
        """Execute a task, re-publishing to RabbitMQ on failure if retries remain."""
        result = self._runner.execute(task)

        if not result.success and task.retry_count < task.max_retries:
            self._retry(task)

        return result

    def _retry(self, task: MigrationTask) -> None:
        """Re-publish a failed task to RabbitMQ with incremented retry count and backoff."""
        backoff = self._conf.executor.retry_backoff * (2 ** task.retry_count)
        retry_task = MigrationTask(
            task_id=task.task_id,
            plan_id=task.plan_id,
            policy_name=task.policy_name,
            aggregate=task.aggregate,
            instance_uuid=task.instance_uuid,
            instance_name=task.instance_name,
            from_host=task.from_host,
            to_host=task.to_host,
            weight=task.weight,
            priority=task.priority,
            max_retries=task.max_retries,
            retry_count=task.retry_count + 1,
            not_before=task.not_before + backoff,
            created_at=task.created_at,
        )
        emit_notification(
            self._migration_notifier,
            "migration.requested",
            task_to_dict(retry_task),
        )
        LOG.info(
            "Re-published migration %s for retry (attempt %d/%d, backoff=%.0fs)",
            task.task_id[:8],
            retry_task.retry_count,
            task.max_retries,
            backoff,
        )

    def _publish_result(self, result: MigrationResult) -> None:
        """Publish a migration result to the results topic."""
        event_type = "migration.completed" if result.success else "migration.failed"
        emit_notification(self._result_notifier, event_type, task_to_dict(result))
        LOG.info(
            "Published %s for task %s (instance %s)",
            event_type,
            result.task_id[:8],
            result.instance_uuid[:8],
        )

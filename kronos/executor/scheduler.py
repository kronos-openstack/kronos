"""Task scheduling: priority queue with stagger and concurrency control.

The scheduler sits between the oslo.messaging consumer and the migration
runner.  It accepts tasks immediately (so the consumer can ack), holds
them in a priority queue sorted by ``not_before``, and dispatches them
to worker threads when their time arrives and a concurrency slot is free.

On executor restart, queued tasks are lost — the engine re-plans next cycle.
"""

from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable

from oslo_log import log as logging

from kronos.engine.types import MigrationResult, MigrationTask

LOG = logging.getLogger(__name__)


class TaskScheduler:
    """Schedules migration tasks with stagger and concurrency limits.

    A single scheduler thread manages the priority queue.  When a task's
    ``not_before`` timestamp arrives and a semaphore slot is available,
    the task is dispatched to a worker thread.
    """

    def __init__(
        self,
        max_concurrent: int,
        on_result: Callable[[MigrationResult], None],
        run_task: Callable[[MigrationTask], MigrationResult],
    ) -> None:
        """
        :param max_concurrent: Maximum simultaneous migrations.
        :param on_result: Callback invoked with each MigrationResult.
        :param run_task: Function that executes a MigrationTask and returns a result.
        """
        self._semaphore = threading.Semaphore(max_concurrent)
        self._on_result = on_result
        self._run_task = run_task

        self._queue: list[tuple[float, int, MigrationTask]] = []
        self._queue_lock = threading.Lock()
        self._queue_event = threading.Event()
        self._counter = 0  # tie-breaker for heap ordering

        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the scheduler thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            name="task-scheduler",
            daemon=True,
        )
        self._thread.start()
        LOG.info("Task scheduler started.")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for it to finish."""
        self._running = False
        self._queue_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        LOG.info("Task scheduler stopped.")

    def submit(self, task: MigrationTask) -> None:
        """Add a task to the scheduling queue.

        :param task: Migration task to schedule.
        """
        with self._queue_lock:
            self._counter += 1
            heapq.heappush(self._queue, (task.not_before, self._counter, task))
        self._queue_event.set()
        LOG.debug(
            "Task %s queued (not_before=%.0f, queue_size=%d)",
            task.task_id[:8],
            task.not_before,
            len(self._queue),
        )

    def _scheduler_loop(self) -> None:
        """Main scheduler loop: wait for tasks, dispatch when ready."""
        while self._running:
            task = self._pop_next_ready()
            if task is None:
                # Wait for new tasks or stop signal
                self._queue_event.wait(timeout=1.0)
                self._queue_event.clear()
                continue

            # Acquire semaphore (blocks if at max concurrency)
            self._semaphore.acquire()
            threading.Thread(
                target=self._worker,
                args=(task,),
                name=f"migration-{task.task_id[:8]}",
                daemon=True,
            ).start()

    def _pop_next_ready(self) -> MigrationTask | None:
        """Pop the next task whose not_before has passed, or None."""
        now = time.time()
        with self._queue_lock:
            if not self._queue:
                return None
            not_before, _, _ = self._queue[0]
            if not_before > now:
                return None
            _, _, task = heapq.heappop(self._queue)
            return task

    def _worker(self, task: MigrationTask) -> None:
        """Execute a task in a worker thread and publish the result."""
        try:
            result = self._run_task(task)
            self._on_result(result)
        except Exception:
            LOG.exception(
                "Unexpected error executing migration %s",
                task.task_id[:8],
            )
        finally:
            self._semaphore.release()

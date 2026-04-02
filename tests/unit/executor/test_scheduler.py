"""Tests for the task scheduler."""

from __future__ import annotations

import threading
import time

from kronos.engine.types import MigrationResult, MigrationTask
from kronos.executor.scheduler import TaskScheduler


def _make_task(
    task_id: str = "task-1",
    not_before: float = 0.0,
) -> MigrationTask:
    return MigrationTask(
        task_id=task_id,
        plan_id="plan-1",
        policy_name="test-policy",
        aggregate="test-agg",
        instance_uuid="uuid-1",
        instance_name="vm-1",
        from_host="h1",
        to_host="h2",
        weight=0.1,
        not_before=not_before,
    )


def _make_result(task: MigrationTask, success: bool = True) -> MigrationResult:
    return MigrationResult(
        task_id=task.task_id,
        plan_id=task.plan_id,
        policy_name=task.policy_name,
        instance_uuid=task.instance_uuid,
        from_host=task.from_host,
        to_host=task.to_host,
        success=success,
    )


class TestTaskScheduler:
    def test_submit_and_dispatch(self) -> None:
        results: list[MigrationResult] = []
        dispatched = threading.Event()

        def run_task(task: MigrationTask) -> MigrationResult:
            result = _make_result(task)
            dispatched.set()
            return result

        scheduler = TaskScheduler(
            max_concurrent=2,
            on_result=results.append,
            run_task=run_task,
        )
        scheduler.start()

        try:
            task = _make_task(not_before=0.0)
            scheduler.submit(task)
            assert dispatched.wait(timeout=5.0)
            # Give on_result callback time to fire
            time.sleep(0.1)
            assert len(results) == 1
            assert results[0].success
        finally:
            scheduler.stop()

    def test_respects_not_before(self) -> None:
        dispatch_times: list[float] = []

        def run_task(task: MigrationTask) -> MigrationResult:
            dispatch_times.append(time.time())
            return _make_result(task)

        scheduler = TaskScheduler(
            max_concurrent=2,
            on_result=lambda r: None,
            run_task=run_task,
        )
        scheduler.start()

        try:
            # Task with not_before 1 second in the future
            future_time = time.time() + 1.0
            task = _make_task(not_before=future_time)
            scheduler.submit(task)

            # Should not dispatch immediately
            time.sleep(0.3)
            assert len(dispatch_times) == 0

            # Wait for it to dispatch
            time.sleep(1.5)
            assert len(dispatch_times) == 1
            assert dispatch_times[0] >= future_time - 0.5  # allow some tolerance
        finally:
            scheduler.stop()

    def test_concurrency_limit(self) -> None:
        running = threading.Semaphore(0)
        gate = threading.Event()
        max_concurrent_seen: list[int] = []
        active_count = 0
        lock = threading.Lock()

        def run_task(task: MigrationTask) -> MigrationResult:
            nonlocal active_count
            with lock:
                active_count += 1
                max_concurrent_seen.append(active_count)
            running.release()
            gate.wait(timeout=5.0)
            with lock:
                active_count -= 1
            return _make_result(task)

        scheduler = TaskScheduler(
            max_concurrent=2,
            on_result=lambda r: None,
            run_task=run_task,
        )
        scheduler.start()

        try:
            # Submit 3 tasks (max_concurrent=2)
            for i in range(3):
                scheduler.submit(_make_task(task_id=f"task-{i}", not_before=0.0))

            # Wait for 2 to start running
            assert running.acquire(timeout=5.0)
            assert running.acquire(timeout=5.0)
            time.sleep(0.3)

            # Third should not have started
            assert max(max_concurrent_seen) <= 2

            # Release the gate
            gate.set()
        finally:
            scheduler.stop()

    def test_ordering_by_not_before(self) -> None:
        order: list[str] = []
        done = threading.Event()

        def run_task(task: MigrationTask) -> MigrationResult:
            order.append(task.task_id)
            if len(order) == 2:
                done.set()
            return _make_result(task)

        scheduler = TaskScheduler(
            max_concurrent=1,
            on_result=lambda r: None,
            run_task=run_task,
        )

        now = time.time()
        # Submit later task first, earlier task second
        scheduler.submit(_make_task(task_id="late", not_before=now + 0.2))
        scheduler.submit(_make_task(task_id="early", not_before=now))

        scheduler.start()
        try:
            assert done.wait(timeout=5.0)
            assert order[0] == "early"
            assert order[1] == "late"
        finally:
            scheduler.stop()

    def test_stop_is_clean(self) -> None:
        scheduler = TaskScheduler(
            max_concurrent=2,
            on_result=lambda r: None,
            run_task=lambda t: _make_result(t),
        )
        scheduler.start()
        scheduler.stop()
        # Should not raise

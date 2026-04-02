# Executor Module

## Purpose
Consumes migration tasks from RabbitMQ and executes them via the Nova
live-migrate API.  The executor never decides *what* to migrate — it
only validates and executes plans produced by the engine.

## Key Files
- `worker.py` — `ExecutorWorker`: top-level wiring (messaging, scheduler, runner); `MigrationEndpoint`: oslo.messaging notification handler
- `scheduler.py` — `TaskScheduler`: priority queue sorted by `not_before`, semaphore for concurrency control
- `migrate.py` — `MigrationRunner`: pre-flight check, Nova live-migrate, poll status, post-flight verify

## Deployment Model
- One active executor per aggregate, optional passive standby (M4: tooz lock)
- Configured with `--aggregate <name>`, subscribes to `kronos.migrations.<aggregate>`
- Publishes results to `kronos.results.<aggregate>`

## Message Flow
```
RabbitMQ (kronos.migrations.<agg>)
    │
    ▼
MigrationEndpoint.info()          ← oslo.messaging auto-acks on return
    │
    ▼
TaskScheduler.submit(task)        ← queued by not_before timestamp
    │
    ▼ (when not_before arrives + semaphore slot free)
    │
MigrationRunner.execute(task)
    ├── pre-flight: ACTIVE? no task_state? still on from_host?
    ├── nova.live_migrate()
    ├── poll get_migration_status() until terminal or timeout
    └── post-flight: on to_host? ACTIVE?
    │
    ▼
MigrationResult → RabbitMQ (kronos.results.<agg>)
```

## Retry Logic
- On failure, if `retry_count < max_retries`, the task is re-published to
  the migrations topic with incremented `retry_count` and exponential backoff
  `not_before` timestamp
- After `max_retries`, the task is logged and dropped (dead letter)
- Retries go back through RabbitMQ, not held locally

## Restart Behaviour
- oslo.messaging auto-acks messages when the handler returns
- If the executor restarts, tasks in the local scheduler queue are lost
- Tasks mid-migration: Nova continues the migration regardless; the engine
  re-evaluates next cycle and sees updated host scores
- This is acceptable because the system is self-healing

## Safety Guarantees
1. Pre-flight check before every migration (plan may be stale)
2. Staggered execution via `not_before` timestamps
3. Concurrency cap via semaphore (`max_concurrent_migrations`)
4. Hard timeout on migration polling
5. Post-flight verification (host + status)
6. Idempotent: pre-flight catches duplicate or stale tasks

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

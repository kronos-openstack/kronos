# Executor Module

## Purpose
Consumes migration tasks via RPC from the engine and executes them via
the Nova live-migrate API. The executor never decides *what* to migrate
— it only validates, executes, and reports results.

## Key Files
- `worker.py` — `ExecutorWorker`: top-level wiring (RPC server, scheduler, runner, result notifier); `MigrationRPCEndpoint`: RPC endpoint exposing `execute_migration`
- `scheduler.py` — `TaskScheduler`: priority queue sorted by `not_before`, semaphore for concurrency control
- `migrate.py` — `MigrationRunner`: pre-flight check, Nova live-migrate, poll status, post-flight verify

## Deployment Model
- One active executor per aggregate (or for the unassigned-hosts pool)
- Started with either `--aggregate <name>` or `--unassigned`
- RPC topic: `kronos.migrations.<aggregate>` (or `kronos.migrations._unassigned_` for the unassigned pool)
- Results topic: `kronos.results.<aggregate>`
- Passive standby possible (same RPC topic → competing consumers)

## Messaging
Two oslo.messaging primitives, each for a reason:

- **Engine → Executor**: RPC cast. Exactly-one delivery via competing
  consumers. If there were ever two executors on the same topic
  (active + passive), only one would handle each task.
- **Executor → Engines**: Notifications. Broadcast so that both
  active and passive engines can update their cooldown state from
  `migration.completed` / `migration.failed` events.

## Message Flow
```
Engine.RPCClient.cast('execute_migration', task={...})
    │
    ▼
RabbitMQ topic (kronos.migrations.<agg>)
    │
    ▼
MigrationRPCEndpoint.execute_migration(ctxt, task)
    │                  ← oslo.messaging acks on handler return
    ▼
TaskScheduler.submit(task)      ← queued by not_before timestamp
    │
    ▼ (when not_before arrives + semaphore slot free)
    │
MigrationRunner.execute(task)
    ├── pre-flight: ACTIVE? no task_state? still on from_host?
    ├── nova.live_migrate()
    ├── poll nova.get_migration_status() until terminal or timeout
    └── post-flight: on to_host? ACTIVE?
    │
    ▼
Notifier.info('migration.completed' | 'migration.failed', result)
    │
    ▼
RabbitMQ topic (kronos.results.<agg>)
    │
    ▼
Engine notification listeners (active + passive) update cooldown state
```

## Retry Logic
- On failure, if `retry_count < max_retries`, the task is **re-cast** to
  the same RPC migrations topic with incremented `retry_count` and
  exponential backoff `not_before` timestamp.
- After `max_retries`, the task is logged and dropped (dead letter).
- Retries go back through RabbitMQ so passive executors could pick them
  up on failover.

## Restart Behaviour
- oslo.messaging acks messages when the handler returns (submission to
  the local scheduler), not when the migration completes.
- If the executor restarts, tasks in the local scheduler queue are lost.
- Tasks mid-migration: Nova continues the migration regardless; the
  engine re-evaluates next cycle and sees updated host scores.
- This is acceptable because the system is self-healing — the engine
  re-plans every `evaluation_interval` seconds.

## Safety Guarantees
1. Pre-flight check before every migration (the plan may be stale)
2. Staggered execution via `not_before` timestamps on each task
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

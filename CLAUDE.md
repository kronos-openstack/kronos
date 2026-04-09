# Kronos — OpenStack VM Placement Optimization Engine

## Project Overview
Kronos is a PromQL-driven VM placement optimization engine for OpenStack.
It evaluates Prometheus metrics per host aggregate and plans live migrations to balance
(or consolidate) workloads. Open-source, Apache 2.0, targeting OpenStack umbrella acceptance.

## Architecture
- **Engine** evaluates policies on a configurable interval. Its scope is a
  list of Nova host aggregates defined in `[engine] aggregates`, optionally
  plus the "unassigned hosts" pool (`[engine] include_unassigned_hosts`).
  Each aggregate is planned independently.
- **Combined scoring**: when multiple policies share the same scope, each
  contributes to a weighted combined imbalance score. Policy weights sum to
  1.0. Spread and pack modes cannot be combined — deploy separate engines
  per mode.
- **Pure planner**: casts migration tasks over RPC (`oslo.messaging`
  `RPCClient.cast()`) to the per-aggregate executor topic. Never calls
  Nova live-migrate directly.
- **Executor**: one per aggregate. Consumes RPC casts, runs pre-flight
  checks, calls Nova live-migrate, polls status, publishes results as
  notifications.
- **Aggregate boundaries**: migrations stay within an aggregate. The
  unassigned pool is its own scope — migrations never cross between it
  and a named aggregate.
- **HA** (planned): active-passive engines via tooz distributed locks.

## OpenStack Conventions — MUST FOLLOW
- **oslo.config** for all daemon configuration (`kronos.conf`)
- **oslo.log** for logging
- **oslo.messaging**:
  - Engine → executor: **RPC cast** on `kronos.migrations.<aggregate>`
    (exactly-one delivery, competing consumers)
  - Executor → engines: **notifications** on
    `kronos.results.<aggregate>` (broadcast to active and passive
    engines for cooldown tracking)
- **openstacksdk** for Nova/Keystone API calls
- Entry points in `pyproject.toml` under `[project.scripts]`
- Config options registered in `kronos/common/config.py`
- Exceptions follow the `msg_fmt` pattern (see `kronos/common/exceptions.py`)

## Config Split
- `/etc/kronos/kronos.conf` — oslo.config INI (daemon settings: intervals, URLs, auth)
- `/etc/kronos/policies.yaml` — Pydantic-validated YAML (PromQL queries, thresholds, weights)

## Logging — oslo.log ONLY
**NEVER use `import logging` from the standard library.** Always use oslo.log:
```python
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
```
oslo.log wraps stdlib logging but integrates with oslo.config for log level,
format, and output configuration. The entry point (`kronos/cmd/*.py`) calls
`logging.setup(CONF, 'kronos')` once at startup — individual modules just
call `logging.getLogger(__name__)`.

## Code Conventions
- Python 3.12+, type hints on all public APIs
- Pydantic v2 for policy YAML validation only (NOT for daemon config)
- Dataclasses for internal data types (HostScore, PolicyResult, etc.) — NOT frozen, for testability
- No global mutable state — dependency injection via constructors
- Ruff for linting (`ruff check`), mypy strict mode
- Tests: pytest, mocked HTTP via `responses` library, mocked openstacksdk

## Package Layout (follows Nova/Neutron pattern)
- `kronos/cmd/` — CLI entry points (one module per binary)
- `kronos/common/` — Shared utilities, exceptions, oslo.config registration, messaging helpers
- `kronos/policies/` — Pydantic models and loader for policy YAML
- `kronos/clients/` — External service clients (Prometheus, Nova)
- `kronos/engine/` — Control loop, scoring, planning, cooldown tracking
- `kronos/executor/` — Migration executor (scheduler, runner, RPC server, result notifier)

## Entry Points
- `kronos-engine` → `kronos.cmd.engine:main` — scheduling engine daemon
- `kronos-test-config` → `kronos.cmd.test_config:main` — config validator
- `kronos-executor` → `kronos.cmd.executor:main` — migration executor daemon
- `kronos-record` → `kronos.cmd.record:main` — snapshot live cluster state
- `kronos-replay` → `kronos.cmd.replay:main` — run engine pipeline offline against a snapshot

## Engine Scope
The engine operates on a fixed set of aggregates defined at startup:

```ini
[engine]
aggregates = gpu-aggregate, hpc-aggregate
include_unassigned_hosts = false
```

Semantics:
- `aggregates` — comma-separated Nova host aggregate names
- `include_unassigned_hosts` — when true, also plan the pool of
  compute hypervisors that belong to no aggregate (common in small
  deployments without aggregate groups)
- Passing `None` to `NovaClient.get_hosts_in_aggregate()` returns the
  unassigned pool
- At least one of `aggregates` or `include_unassigned_hosts=true`
  must be set or the engine fails at startup

## Running Tests
```bash
pip install -e ".[dev]"
pytest tests/
ruff check kronos/ tests/
mypy kronos/
```

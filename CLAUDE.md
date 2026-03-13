# Kronos — OpenStack VM Placement Optimization Engine

## Project Overview
Kronos is a PromQL-driven VM placement optimization engine for OpenStack.
It evaluates Prometheus metrics per host aggregate and plans live migrations to balance
(or consolidate) workloads. Open-source, Apache 2.0, targeting OpenStack umbrella acceptance.

## Architecture
- **Engine** evaluates policies on a configurable interval (one engine per aggregate)
- **Pure planner**: emits migration plans to RabbitMQ (M3+), never directly calls Nova migrate
- **Executor** (M3+): dedicated oslo.messaging consumer that calls Nova live-migrate API
- **Per-aggregate**: each policy targets a Nova host aggregate, migrations stay within boundaries
- **HA** (M4+): active-passive engines via tooz distributed locks

## OpenStack Conventions — MUST FOLLOW
- **oslo.config** for all daemon configuration (`kronos.conf`)
- **oslo.log** for logging
- **oslo.messaging** for RPC/notifications (M3+)
- **Stevedore** for plugin discovery (M2+)
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
- `kronos/common/` — Shared utilities, exceptions, oslo.config registration
- `kronos/policies/` — Pydantic models and loader for policy YAML
- `kronos/clients/` — External service clients (Prometheus, Nova)
- `kronos/engine/` — Control loop, scoring, planning (M1-M2)
- `kronos/executor/` — Migration executor (M3+)
- `kronos/api/` — REST API (M5+)
- `kronos/coordination/` — HA and rate limiting (M4+)

## Entry Points
- `kronos-engine` → `kronos.cmd.engine:main` — scheduling engine daemon
- `kronos-test-config` → `kronos.cmd.test_config:main` — config validator
- `kronos-executor` → `kronos.cmd.executor:main` (M3+)
- `kronos-api` → `kronos.cmd.api:main` (M5+)

## Milestones
- **M1** (current): Skeleton — oslo.config, clients, dry-run engine loop
- **M2**: Scoring + planner — simulation-based migration planning, dry-run reports
- **M3**: Queue + executor — oslo.messaging, migration lifecycle, retries
- **M4**: HA — tooz locks, active-passive, distributed rate limiter
- **M5**: API + persistence — REST API, policy CRUD, audit log
- **M6**: Packaging — PyPI, systemd units, docs

## Running Tests
```bash
pip install -e ".[dev]"
pytest tests/
ruff check kronos/ tests/
mypy kronos/
```

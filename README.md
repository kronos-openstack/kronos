# Kronos

**PromQL-driven VM placement optimization engine for OpenStack**

Kronos evaluates Prometheus metrics per Nova host aggregate and plans live migrations
to balance (spread) or consolidate (pack) workloads. When dry-run is disabled, it emits
migration plans to RabbitMQ via oslo.messaging and a dedicated executor daemon carries
them out through the Nova live-migrate API.

> **Status:** Pre-alpha (Milestone 3 — queue-based executor). Not yet ready for production.

## How It Works

```
              +-----------+       +------+       +----------+
              | Prometheus|       | Nova |       | RabbitMQ |
              +-----+-----+       +---+--+       +-----+----+
                    |                 |                 |
              PromQL queries    host aggregates         |
                    |                 |                 |
              +-----v-----------------v--+              |
              |       kronos-engine       |              |
              |  per-policy evaluation:   |              |
              |  query → score → profile  |              |
              |  → plan migrations        |              |
              +------------+--------------+              |
                           |                             |
                  MigrationTask (per step)               |
                  via oslo.messaging ─────────────────►  |
                                                         |
              +------------------------------------------v--+
              |            kronos-executor                   |
              |  consume → pre-flight → live-migrate → poll |
              |  → post-flight → publish result             |
              +---------------------------------------------+
```

1. **Policies** define PromQL queries, thresholds, and scheduling modes per host aggregate.
2. **Scorer** queries Prometheus, matches results to Nova hosts, normalizes scores, and detects imbalance.
3. **Profiler** collects per-VM resource weights from Prometheus with fallback strategies for missing data.
4. **Planner** simulates VM migrations to find an optimal rebalancing plan (greedy spread, FFD pack).
5. **Cooldown Tracker** prevents oscillation by enforcing policy-level and instance-level cooldown periods.
6. **Executor** consumes migration tasks from RabbitMQ, validates pre-flight state, calls Nova live-migrate, polls until completion, and verifies post-flight.

## Quick Start

### Prerequisites

- Python 3.12+
- OpenStack cloud with Nova and Keystone
- Prometheus with host-level metrics (e.g., `node_exporter`, `libvirt_exporter`)
- RabbitMQ — the existing OpenStack broker; only needed when `dry_run = false`

### Install

```bash
git clone https://github.com/kronos-openstack/kronos.git
cd kronos
pip install -e .
```

### Configure

Kronos uses two configuration files:

| File | Format | Purpose |
|------|--------|---------|
| `kronos.conf` | INI (oslo.config) | Daemon settings: intervals, Prometheus URL, Nova auth, messaging, executor |
| `policies.yaml` | YAML (Pydantic) | PromQL queries, thresholds, scheduling modes |

Copy the samples and edit them:

```bash
sudo mkdir -p /etc/kronos
sudo cp etc/kronos/kronos.conf.sample /etc/kronos/kronos.conf
sudo cp etc/kronos/policies.yaml.sample /etc/kronos/policies.yaml
```

**Minimal `kronos.conf`:**

```ini
[engine]
evaluation_interval = 60
dry_run = true
policies_file = /etc/kronos/policies.yaml

[prometheus]
url = http://prometheus:9090

[nova]
auth_type = password
auth_url = http://keystone:5000/v3
username = kronos
password = secret
project_name = service
user_domain_name = Default
project_domain_name = Default

[messaging]
transport_url = rabbit://guest:guest@localhost:5672/

[executor]
max_concurrent_migrations = 2
migration_timeout = 1800
max_retries = 3
stagger_seconds = 30
```

**Minimal `policies.yaml`:**

```yaml
policies:
  - name: cpu-spread
    mode: spread
    aggregate: my-aggregate
    imbalance_query: |
      1 - avg by (nodename) (
        rate(node_cpu_seconds_total{mode="idle"}[5m])
      ) * on(nodename) group_left()
        node_uname_info
    host_label: nodename
    vm_profile_query: |
      rate(libvirt_domain_info_cpu_time_seconds_total[5m])
      * on(domain, instance) group_left(instance_id)
        libvirt_domain_openstack_info
    vm_profile_label: instance_id
    vm_profile_label_type: nova_instance_uuid
    vm_profile_fallback: host_average
    threshold: 0.15
    cooldown: 10m
    max_migrations_per_cycle: 3
```

### Run

```bash
# Validate configuration and test connectivity
kronos-test-config --config-file /etc/kronos/kronos.conf

# Start the engine (dry-run by default)
kronos-engine --config-file /etc/kronos/kronos.conf

# Start the executor for a specific aggregate (requires dry_run = false)
kronos-executor --config-file /etc/kronos/kronos.conf --aggregate my-aggregate
```

### Record & Replay (offline testing)

Capture a snapshot of live OpenStack + Prometheus state and replay it locally:

```bash
# Record
kronos-record --config-file /etc/kronos/kronos.conf --output-dir /tmp/snapshot

# Replay a single engine cycle against recorded data
kronos-replay --config-file /etc/kronos/kronos.conf --snapshot-dir /tmp/snapshot
```

## Policy Modes

| Mode | Behavior |
|------|----------|
| `spread` | Balance load evenly across hosts — greedy simulation picks the best single move per round |
| `pack` | Consolidate VMs onto fewer hosts — First Fit Decreasing, coldest hosts drained first |

Each policy is scoped to a single Nova host aggregate. Migrations never cross aggregate boundaries.

## Architecture

### Engine (planner)

One engine per aggregate evaluates policies on a configurable interval:

1. **Score** — query Prometheus, normalize per-host scores 0.0–1.0, detect imbalance
2. **Profile** — collect per-VM resource weights (Prometheus + fallback strategies)
3. **Constrain** — validate moves against Nova server group anti-affinity rules
4. **Plan** — simulate moves to minimize imbalance (spread) or maximize consolidation (pack)
5. **Emit** — publish `MigrationTask` messages to `kronos.migrations.<aggregate>` via oslo.messaging
6. **Cooldown** — enforce policy-level and instance-level cooldown to prevent oscillation

### Executor (migration runner)

One executor per aggregate consumes tasks from RabbitMQ:

1. **Schedule** — priority queue sorted by `not_before` timestamps, semaphore for concurrency
2. **Pre-flight** — verify instance is ACTIVE, no pending task_state, still on source host
3. **Migrate** — call Nova live-migrate API
4. **Poll** — check migration status until terminal state or timeout
5. **Post-flight** — confirm instance landed on destination host and is ACTIVE
6. **Retry** — on failure, re-publish with exponential backoff (up to `max_retries`)
7. **Report** — publish `MigrationResult` to `kronos.results.<aggregate>`

### Messaging topology

| Topic | Publisher | Consumer |
|-------|-----------|----------|
| `kronos.migrations.<aggregate>` | Engine | Executor |
| `kronos.results.<aggregate>` | Executor | Engine (active + passive, for cooldown tracking) |

## Project Layout

```
kronos/
├── cmd/           CLI entry points (kronos-engine, kronos-executor, kronos-test-config, kronos-record, kronos-replay)
├── common/        Shared utilities, exceptions, oslo.config registration, oslo.messaging helpers
├── policies/      Pydantic models and YAML loader for policy definitions
├── clients/       Prometheus HTTP client, Nova/OpenStack client (read + live-migrate)
├── engine/        Control loop, scoring, profiling, constraint checking, planning, cooldown tracking
└── executor/      Migration executor: worker, scheduler, migration runner
```

## Development

```bash
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check kronos/ tests/

# Type check
mypy kronos/
```

## Roadmap

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1** | Project skeleton, oslo.config, clients, dry-run engine loop | Done |
| **M2** | VM profiling, simulation-based migration planning, constraint checking, record/replay | Done |
| **M3** | oslo.messaging queue, migration executor, cooldown tracking | Done |
| **M4** | HA via tooz distributed locks, active-passive, rate limiting | Planned |
| **M5** | REST API, policy CRUD, audit logging | Planned |
| **M6** | PyPI packaging, systemd units, documentation | Planned |

## License

Apache 2.0 — see [LICENSE](LICENSE).

# Kronos

**PromQL-driven VM placement optimization engine for OpenStack**

Kronos evaluates Prometheus metrics per Nova host aggregate and plans live migrations
to balance (spread) or consolidate (pack) workloads. Multiple policies on the same
aggregate are combined into a single weighted score, so the planner can trade off
memory and CPU (or any other PromQL-driven dimensions) simultaneously.

When dry-run is disabled, the engine casts migration tasks to a per-aggregate RPC
topic via oslo.messaging. A dedicated executor daemon consumes the tasks and carries
them out through the Nova live-migrate API.

> **Status:** Pre-alpha. Not yet ready for production.

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
              |  for each aggregate:      |              |
              |    score all policies     |              |
              |    combined imbalance     |              |
              |    profile all VMs        |              |
              |    enforce affinity rules |              |
              |    plan combined moves    |              |
              +------------+--------------+              |
                           |                             |
                  MigrationTask per step                 |
                  RPC cast ───────────────────────────►  |
                                                         |
              +------------------------------------------v--+
              |            kronos-executor                   |
              |  consume → pre-flight → live-migrate → poll |
              |  → post-flight → publish result             |
              +---------------------------------------------+
```

1. **Policies** define PromQL queries, thresholds, and scheduling modes. All
   policies in one file apply to every aggregate the engine manages.
2. **Scorer** runs each policy's PromQL imbalance query against the aggregate's
   host list, enforces the [0, 1] contract, and detects imbalance.
3. **Profiler** collects per-VM resource weights *across all policies* in one
   pass. Each VM carries a per-policy weight dict.
4. **Combined scoring**: the planner simulates moves against every policy's
   scores simultaneously, minimizing a weighted sum of imbalances (policy
   `weight` values sum to 1.0).
5. **Constraint checker** respects all four Nova server-group placement
   policies: `affinity`, `anti-affinity`, `soft-affinity`, and
   `soft-anti-affinity`. A move that would break any of them is rejected.
6. **Affinity enforcer** (optional) runs before the planner and proposes
   migrations to repair existing server-group violations. Enabled per
   policy class via `[engine] enforce_hard_affinity` and
   `enforce_soft_affinity`. Destinations are picked to minimise the
   combined imbalance and never cross a policy threshold. Repair and
   imbalance moves share a single `max_migrations_per_cycle` budget.
7. **Cooldown tracker** prevents oscillation via aggregate-level and
   instance-level cooldowns.
8. **Executor** consumes migration tasks, validates pre-flight state, calls
   Nova live-migrate, polls until completion, and verifies post-flight.

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

# Aggregate scope: at least one of `aggregates` or
# `include_unassigned_hosts = true` must be set.
aggregates = my-aggregate
include_unassigned_hosts = false

# Cooldowns (seconds)
cooldown = 600
instance_cooldown = 900

# Optional: repair existing server-group violations every cycle.
# Both off by default.
enforce_hard_affinity = false
enforce_soft_affinity = false

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

Aggregates live on the engine (`[engine] aggregates`), not the policy.
Enabled policy weights must sum to 1.0. All policies in one file must
share a mode (spread or pack).

```yaml
policies:
  - name: cpu-spread
    mode: spread
    weight: 0.3
    imbalance_query: |
      1 - avg by (nodename) (
        rate(node_cpu_seconds_total{mode="idle"}[5m])
        * on(instance) group_left(nodename)
          node_uname_info
      )
    host_label: nodename
    vm_profile_query: |
      rate(libvirt_domain_info_cpu_time_seconds_total[5m])
      * on(domain, instance) group_left(instance_id)
        libvirt_domain_openstack_info
    vm_profile_label: instance_id
    vm_profile_label_type: nova_instance_uuid
    vm_profile_fallback: host_average
    threshold: 0.05
    max_migrations_per_cycle: 3

  - name: memory-spread
    mode: spread
    weight: 0.7
    imbalance_query: |
      1 - avg by (nodename) (
        node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes
        * on(instance) group_left(nodename)
          node_uname_info
      )
    host_label: nodename
    vm_profile_query: |
      libvirt_domain_memory_stats_rss_bytes
      / on(instance) group_left()
        label_replace(node_memory_MemTotal_bytes, "instance", "$1:9177", "instance", "(.+):.*")
      * on(domain, instance) group_left(instance_id)
        libvirt_domain_openstack_info
    vm_profile_label: instance_id
    vm_profile_label_type: nova_instance_uuid
    vm_profile_fallback: skip
    threshold: 0.10
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

# Or for the unassigned-hosts pool (clusters without aggregates)
kronos-executor --config-file /etc/kronos/kronos.conf --unassigned
```

### Record & Replay (offline testing)

Capture a snapshot of live OpenStack + Prometheus state and replay it locally:

```bash
# Record
kronos-record --config-file /etc/kronos/kronos.conf /tmp/snapshot

# Replay a single engine cycle against recorded data
kronos-replay --config-file /etc/kronos/kronos.conf /tmp/snapshot
```

## Policy Modes

| Mode | Behavior |
|------|----------|
| `spread` | Balance load evenly across hosts — greedy combined-score simulation picks the best single move per round |
| `pack` | Consolidate VMs onto fewer hosts — First Fit Decreasing on combined utilization |

All policies in one file must share a mode. Migrations never cross
aggregate boundaries.

## Architecture

### Engine (planner)

One engine owns a set of aggregates (or the unassigned-hosts pool) and
evaluates all enabled policies against each aggregate every cycle:

1. **Score** — each policy runs its PromQL imbalance query; values must be in [0, 1]
2. **Profile** — collect per-VM resource weights *across all policies* in one pass
3. **Constrain** — reject any move that would break a Nova server-group placement rule
4. **Enforce** (optional) — when `enforce_hard_affinity` / `enforce_soft_affinity` is set,
   propose repair moves for VMs already violating their groups
5. **Plan** — simulate moves minimizing the weighted combined imbalance, sharing the
   per-cycle migration budget with the enforcer
6. **Cast** — send `MigrationTask` over RPC to `kronos.migrations.<aggregate>`. Each
   task carries a `phase` field (`affinity`, `spread`, or `pack`) that surfaces in
   logs so operators can see why each migration was proposed
7. **Cooldown** — enforce aggregate-level and instance-level cooldown

### Executor (migration runner)

One executor per aggregate consumes tasks from RabbitMQ:

1. **Schedule** — priority queue sorted by `not_before` timestamps, semaphore for concurrency
2. **Pre-flight** — verify instance is ACTIVE, no pending task_state, still on source host
3. **Migrate** — call Nova live-migrate API
4. **Poll** — check migration status until terminal state or timeout
5. **Post-flight** — confirm instance landed on destination host and is ACTIVE
6. **Retry** — on failure, re-cast with exponential backoff (up to `max_retries`)
7. **Report** — publish `MigrationResult` notification on `kronos.results.<aggregate>`

### Messaging topology

| Topic | Primitive | Publisher | Consumer |
|-------|-----------|-----------|----------|
| `kronos.migrations.<aggregate>` | RPC cast | Engine | Executor (competing consumers) |
| `kronos.results.<aggregate>` | Notification | Executor | Engines (broadcast to active + passive for cooldown tracking) |

The unassigned-hosts pool uses the reserved name `_unassigned_` in its topics.

## Project Layout

```
kronos/
├── cmd/           CLI entry points (kronos-engine, kronos-executor, kronos-test-config, kronos-record, kronos-replay)
├── common/        Shared utilities, exceptions, oslo.config registration, oslo.messaging helpers
├── policies/      Pydantic models and YAML loader for policy definitions
├── clients/       Prometheus HTTP client, Nova/OpenStack client (read + live-migrate)
├── engine/        Control loop, scoring, profiling, constraint checking, affinity enforcement, planning, cooldown tracking
└── executor/      Migration executor: worker, scheduler, migration runner

tools/             Operational helpers (e.g. generate_fake_snapshot.py for benchmarks)
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

## Benchmarks

Generate a synthetic snapshot in the same shape `kronos-record` writes,
then replay it with timings to measure planner performance without
needing a real cluster:

```bash
python tools/generate_fake_snapshot.py \
    --hosts 50 --vms 5000 --groups 100 --seed 42 \
    /tmp/snapshot-fake

# Point [engine] policies_file at /tmp/snapshot-fake/policies.yaml,
# then:
kronos-replay --config-file /tmp/kronos.conf --time /tmp/snapshot-fake
```

`--time` prints per-phase wall-clock timings (scorer, profiler,
enforcer, planner) so you can see where cycles are spent.

## Roadmap

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1** | Project skeleton, oslo.config, clients, dry-run engine loop | Done |
| **M2** | VM profiling, simulation-based migration planning, constraint checking, record/replay | Done |
| **M3** | oslo.messaging queue, migration executor, cooldown tracking | Done |
| **M3.5** | Affinity enforcer, all four server-group policies, phase-tagged steps, planner perf, benchmarks | Done |
| **M4** | HA via tooz distributed locks, active-passive engines and executors, distributed rate limiting | Planned |
| **M5** | Audit logging (append-only JSONL) and general logging cleanup | Planned |
| **M6** | PyPI packaging, container image, systemd units, documentation | Planned |

Independent named milestones (any order):

| Name | Scope |
|------|-------|
| Pack-rework | Pack mode redesign + compactor parity (`post_drain_action`, `ha_reserve`, host re-enable) |
| Executor-timeout | Drop the executor's migration timeout and rely on Nova's |
| Executor-multi-aggregate | One executor process can service multiple aggregates (one thread per topic) |

## License

Apache 2.0 — see [LICENSE](LICENSE).

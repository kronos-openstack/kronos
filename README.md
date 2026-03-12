# Kronos

**PromQL-driven VM placement engine for OpenStack** — the open-source equivalent of VMware DRS.

Kronos evaluates Prometheus metrics per Nova host aggregate and plans live migrations
to balance (spread) or consolidate (pack) workloads. It is a pure planner: it decides
*what* to move, then delegates execution to a dedicated migration executor.

> **Status:** Pre-alpha (Milestone 1 — dry-run engine loop). Not yet ready for production.

## How It Works

```
                  +-----------+       +------+
                  | Prometheus|       | Nova |
                  +-----+-----+       +---+--+
                        |                 |
                  PromQL queries    host aggregates
                        |                 |
                  +-----v-----------------v--+
                  |       kronos-engine       |
                  |  per-policy evaluation:   |
                  |  query → score → detect   |
                  |     imbalance → plan      |
                  +---------------------------+
```

1. **Policies** define PromQL queries, thresholds, and scheduling modes per host aggregate.
2. **Engine** evaluates each enabled policy on a configurable interval.
3. **Scorer** queries Prometheus, matches results to Nova hosts, normalizes scores, and detects imbalance.
4. **Planner** (M2+) simulates VM migrations to find an optimal rebalancing plan.
5. **Executor** (M3+) consumes plans from RabbitMQ and calls the Nova live-migrate API.

## Quick Start

### Prerequisites

- Python 3.12+
- Access to a Prometheus instance with host-level metrics (e.g., libvirt exporter)
- Access to an OpenStack cloud with Nova and Keystone

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
| `kronos.conf` | INI (oslo.config) | Daemon settings: intervals, Prometheus URL, Nova auth |
| `policies.yaml` | YAML (Pydantic) | PromQL queries, thresholds, scheduling modes |

Copy the samples and edit them:

```bash
sudo mkdir -p /etc/kronos
sudo cp etc/kronos/kronos.conf.sample /etc/kronos/kronos.conf
sudo cp etc/kronos/policies.yaml.sample /etc/kronos/policies.yaml
```

**Minimal `kronos.conf`:**

```ini
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
```

**Minimal `policies.yaml`:**

```yaml
policies:
  - name: cpu-spread
    mode: spread
    aggregate: my-aggregate
    imbalance_query: |
      avg by (host) (
        rate(libvirt_domain_info_cpu_time_seconds_total[5m])
        / on(host) group_left()
          sum by (host) (libvirt_domain_info_virtual_cpus)
      )
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
```

## Policy Modes

| Mode | Behavior |
|------|----------|
| `spread` | Balance load evenly across hosts in the aggregate |
| `pack` | Consolidate VMs onto fewer hosts (requires `capacity_query`) |

Each policy is scoped to a single Nova host aggregate. Migrations never cross aggregate boundaries.

## Project Layout

```
kronos/
├── cmd/           CLI entry points (kronos-engine, kronos-test-config)
├── common/        Shared utilities, exceptions, oslo.config registration
├── policies/      Pydantic models and YAML loader for policy definitions
├── clients/       Prometheus HTTP client, Nova/OpenStack client
└── engine/        Control loop, scoring, imbalance detection
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

| Milestone | Scope |
|-----------|-------|
| **M1** (current) | Project skeleton, oslo.config, clients, dry-run engine loop |
| **M2** | Simulation-based migration planning, dry-run reports |
| **M3** | oslo.messaging queue, migration executor, lifecycle management |
| **M4** | HA via tooz distributed locks, active-passive, rate limiting |
| **M5** | REST API, policy CRUD, audit logging |
| **M6** | PyPI packaging, systemd units, documentation |

## License

Apache 2.0 — see [LICENSE](LICENSE).

# Kronos - OpenStack VM Placement Optimization Engine

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
  1.0. Spread and pack modes cannot be combined - deploy separate engines
  per mode.
- **Pure planner**: casts migration tasks over RPC (`oslo.messaging`
  `RPCClient.cast()`) to the per-aggregate executor topic. Never calls
  Nova live-migrate directly.
- **Executor**: one per aggregate. Consumes RPC casts, runs pre-flight
  checks, calls Nova live-migrate, polls status, publishes results as
  notifications.
- **Aggregate boundaries**: migrations stay within an aggregate. The
  unassigned pool is its own scope - migrations never cross between it
  and a named aggregate.
- **AZ scope**: each engine is bound to exactly one Nova
  availability zone via `[engine] availability_zone` (default
  `nova`). Hosts whose `nova-compute` service reports a different
  zone (or no zone at all) are filtered out of every aggregate
  scope, so migrations cannot cross AZ boundaries by construction.
  Deploy one engine per AZ, the same way you already deploy one
  executor per aggregate. There is no cross-AZ knob: cross-AZ live
  migration is never attempted.
- **On-demand snapshots**: send `SIGUSR1` to the engine to capture a
  full Nova + Prometheus snapshot under `[engine] snapshot_dir`
  (default empty, which disables the feature). When set, the
  directory is created at startup with a writability probe; the
  engine refuses to start if either step fails. Each snapshot lands
  in a fresh `kronos-engine-snapshot-<UTC>` subfolder in the same
  on-disk format as `kronos-record`, so it can be fed straight to
  `kronos-replay`. Both the CLI and the engine call
  `kronos.common.snapshot.write_snapshot()` so the format stays in
  lockstep.
- **Host liveness gate**: every cycle the engine fetches Nova
  `os-services` once and installs the result on the constraint
  checker. Only hosts whose `nova-compute` service is `state=up` and
  `status=enabled` (and not `forced_down`) are accepted as
  destinations. The constraint checker fails closed when service
  state has not been installed - the engine always installs it
  right after `invalidate_cache()`. The executor re-checks at
  pre-flight to catch state changes between dispatch and execution.
- **Evacuator**: when `[engine] evacuate_disabled_hosts = true`,
  VMs on hosts whose service is `status=disabled` (but still up)
  are evacuated. Runs *before* the affinity enforcer and the
  imbalance planner; all three share `max_migrations_per_cycle` as
  one budget. Hosts with `state=down` are not evacuated - live
  migration cannot drain them. Storage is intentionally not
  validated; Nova's `block_migration='auto'` decides per-VM.

## OpenStack Conventions - MUST FOLLOW
- **oslo.config** for all daemon configuration (`kronos.conf`)
- **oslo.log** for logging
- **oslo.messaging**:
  - Engine → executor: **RPC cast** on `kronos.migrations.<aggregate>`
    (exactly-one delivery, competing consumers)
  - Executor → engine: **notifications** on
    `kronos.results.<aggregate>` (drives cooldown and quarantine state)
- **openstacksdk** for Nova/Keystone API calls
- Entry points in `pyproject.toml` under `[project.scripts]`
- Config options registered in `kronos/common/config.py`
- Exceptions follow the `msg_fmt` pattern (see `kronos/common/exceptions.py`)

## Config Split
- `/etc/kronos/kronos.conf` - oslo.config INI (daemon settings: intervals, URLs, auth)
- `/etc/kronos/policies.yaml` - Pydantic-validated YAML (PromQL queries, thresholds, weights)

## Logging - oslo.log ONLY
**NEVER use `import logging` from the standard library.** Always use oslo.log:
```python
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
```
oslo.log wraps stdlib logging but integrates with oslo.config for log level,
format, and output configuration. The entry point (`kronos/cmd/*.py`) calls
`logging.setup(CONF, 'kronos')` once at startup - individual modules just
call `logging.getLogger(__name__)`.

## Code Conventions
- Python 3.12+, type hints on all public APIs
- Pydantic v2 for policy YAML validation only (NOT for daemon config)
- Dataclasses for internal data types (HostScore, PolicyResult, etc.) - NOT frozen, for testability
- No global mutable state - dependency injection via constructors
- Ruff for linting (`ruff check`), mypy strict mode
- Tests: pytest, mocked HTTP via `responses` library, mocked openstacksdk

## Package Layout (follows Nova/Neutron pattern)
- `kronos/cmd/` - CLI entry points (one module per binary)
- `kronos/common/` - Shared utilities, exceptions, oslo.config registration, messaging helpers
- `kronos/policies/` - Pydantic models and loader for policy YAML
- `kronos/clients/` - External service clients (Prometheus, Nova)
- `kronos/engine/` - Control loop, scoring, planning, cooldown tracking
- `kronos/executor/` - Migration executor (scheduler, runner, RPC server, result notifier)

## Entry Points
- `kronos-engine` → `kronos.cmd.engine:main` - scheduling engine daemon
- `kronos-test-config` → `kronos.cmd.test_config:main` - config validator
- `kronos-executor` → `kronos.cmd.executor:main` - migration executor daemon
- `kronos-record` → `kronos.cmd.record:main` - snapshot live cluster state
- `kronos-replay` → `kronos.cmd.replay:main` - run engine pipeline offline against a snapshot

## Engine Scope
The engine is bound to one Nova availability zone and operates on
a fixed set of aggregates within it, defined at startup:

```ini
[engine]
availability_zone = nova
aggregates = gpu-aggregate, hpc-aggregate
include_unassigned_hosts = false
```

Semantics:
- `availability_zone` - the Nova AZ this engine manages. Hosts whose
  nova-compute service reports a different zone (or no zone) are
  dropped from every aggregate scope, so migrations stay within this
  AZ by construction. Default `nova` matches the Nova default zone.
  Deploy one engine per AZ.
- `aggregates` - comma-separated Nova host aggregate names
- `include_unassigned_hosts` - when true, also plan the pool of
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

---

# Module Reference

## kronos/policies - Policy YAML loading

Pydantic v2 models for the policy DSL. Policies contain PromQL query
strings, mode-specific fields (`capacity_query` for pack mode), and
cross-field validation - richer than oslo.config's flat key-value model.
oslo.config handles daemon config; Pydantic handles the policy file.

**Key files:**
- `models.py` - `PolicyConfig`, `PoliciesConfig`
- `loader.py` - YAML file loading, returns validated `PoliciesConfig`

**Policy fields:**
- `name`: unique identifier (lowercase, alphanumeric + hyphens/underscores)
- `mode`: `spread` or `pack`
- `weight`: 0.0-1.0, contribution to combined aggregate imbalance
- `imbalance_query`: PromQL returning a per-host utilization ratio in [0, 1]
- `host_label`: Prometheus label identifying each host (default `host`)
- `vm_profile_query`: PromQL returning a per-VM metric for planner simulation
- `vm_profile_label` / `vm_profile_label_type`: how to map Prometheus labels to Nova instances
- `vm_profile_fallback`: `skip` / `flavor_vcpu_ratio` / `host_average`
- `threshold`: imbalance threshold (max - min) to trigger rebalancing
- `capacity_query`: PromQL for pack mode capacity check (required for pack)
- `capacity_threshold`: max host utilization ceiling for pack mode
- `max_migrations_per_cycle`: cap per evaluation cycle
- `enabled`: whether the policy is active

**Aggregates are NOT in the policy.** They live on the engine via
`[engine] aggregates` and `[engine] include_unassigned_hosts`. A
policies.yaml binds to the engine that loads it and applies to every
aggregate in that engine's scope.

**`PoliciesConfig` validators** (load-time invariants):
- Unique `name` across policies
- All policies share a `mode` (spread and pack cannot mix in one file)
- Enabled `weight` values sum to 1.0 exactly (1e-6 tolerance)

**The [0, 1] contract**: `imbalance_query` must return values in [0, 1].
Enforced at runtime by the scorer - out-of-range values cause that
policy to be skipped for the cycle with an error logged. Not enforced
at load time because the query has to run against live Prometheus.

**Adding a new policy field:**
1. Add to `PolicyConfig` in `models.py` with `Field(...)`
2. Add cross-field validation via `@model_validator` if needed
3. Update the sample `policies.yaml` in `internal-documentation/`
4. Add tests in `tests/unit/policies/test_models.py`
5. Update this section

---

## kronos/clients - Prometheus + Nova

Wrappers for external services. Clients return data; they never make
decisions. Retry logic lives in the client, not the caller.

### Prometheus client (`prometheus.py`)
- Uses `requests` directly (NOT `prometheus-api-client` - poorly maintained)
- Endpoints: `/api/v1/query` (instant), `/api/v1/query_range` (range), `/api/v1/status/runtimeinfo` (health)
- `PrometheusHealth` enum: `HEALTHY`, `STALE`, `PARTIAL`, `UNREACHABLE`
- `instant_query(query, label_key, expected_labels) -> QueryResult`
  - `label_key`: which Prometheus label to use as dict key (default `host`)
  - `expected_labels`: set of expected values (e.g. Nova hostnames) for partial-data detection
- Retries via `tenacity` with exponential backoff
- Auth: bearer token (string or file), optional CA cert
- Config source: oslo.config `[prometheus]` group

### Nova client (`nova.py`)
- `openstacksdk` with a `keystoneauth1` session loaded from oslo.config
- Auth via `[nova]` config: `auth_type`, `auth_url`, `username`, `password`, etc.
- `ks_loading.load_auth_from_conf_options()` + `load_session_from_conf_options()`
- Returns dataclasses (`ComputeHost`, `Instance`, `HostAggregate`, `MigrationStatus`), not raw openstacksdk objects
- **Read**: `list_aggregates()`, `get_aggregate()`, `list_compute_hosts()`,
  `list_instances_on_host()`, `list_server_groups()`,
  `list_compute_services()`,
  `get_hosts_in_aggregate(name | None)` - `None` returns hypervisors
  not in any aggregate (the unassigned pool). Only QEMU/KVM
  hypervisors are returned; ironic bare-metal nodes are filtered out.
- **Write**: `live_migrate()`, `get_instance_status()`,
  `get_instance_host()`, `get_migration_status()`
- **`list_compute_services()`**: returns `ComputeService` dataclasses
  for every `nova-compute` entry in `os-services`. Each has `host`,
  `binary`, `state` (`up`/`down`), `status` (`enabled`/`disabled`),
  `zone` (the host's availability zone, empty string if Nova reports
  none - logged as a warning), `disabled_reason`, `forced_down`. The
  helper `is_available_destination` is True only when up + enabled
  and not forced down. Other binaries (conductor/scheduler) are
  filtered out.
- **Server group compatibility**: reads both legacy `policy` (singular
  string) and modern `policies` (list) and merges them. Older Nova
  deployments populate only the singular field.
- Server groups are listed across **all projects** (`all_projects=True`)
  so the constraint checker sees every group the cluster cares about.

### Testing
- Prometheus: `responses` library to mock HTTP at transport level
- Nova: `unittest.mock.patch` on `openstack.connect()`
- Fixtures in `tests/fixtures/prometheus_responses/`

### Rules
- Clients NEVER make decisions - return data, the engine decides
- All exceptions inherit from `KronosException`
- Exceptions use the `msg_fmt` pattern with `%(placeholder)s` formatting
- No caching in clients themselves (constraint checker caches server groups once per cycle)

---

## kronos/engine - Control loop, scoring, planning

Main control loop, policy evaluation, VM profiling, constraint checking,
combined-scoring migration planning, and cooldown tracking. The engine
is a pure planner - it evaluates policies, detects imbalance, simulates
moves, produces migration plans, and casts them over RPC. It never
calls Nova live-migrate directly.

### Key files
- `types.py` - `HostScore`, `VmProfile`, `MigrationStep`, `MigrationPlan`, `MigrationTask`, `MigrationResult`, `MigrationPhase`, `PolicyResult`, `AggregateResult`, `CycleReport`
- `_sim.py` - Shared simulation helpers (`combined_imbalance`, `simulate_move`, `ScoreState`) used by planner and enforcer
- `scorer.py` - `PolicyScorer`: runs a policy's PromQL `imbalance_query` against a host list, normalises scores, enforces the [0, 1] contract
- `profiler.py` - `VmProfiler`: collects per-VM resource weights across all policies for an aggregate in one pass
- `planner.py` - `Planner`: combined-scoring simulation (spread greedy + pack First Fit Decreasing)
- `constraints.py` - `ConstraintChecker`: validates moves against all four Nova server group placement policies (affinity, anti-affinity, soft-affinity, soft-anti-affinity)
- `affinity_enforcer.py` - `AffinityEnforcer`: repair pass that moves VMs out of existing server-group violations using the same combined-imbalance math the planner uses
- `cooldown.py` - `CooldownTracker`: aggregate-level cooldown, instance-level cooldown, post-failure quarantine
- `result_listener.py` - `MigrationResultEndpoint`: oslo.messaging notification endpoint that consumes `migration.completed` / `migration.failed` events and drives quarantine
- `evacuator.py` - `Evacuator`: drains VMs off `status=disabled`
  nova-compute hosts. Runs before the affinity enforcer and the
  planner; uses the same combined-imbalance + threshold math
- `loop.py` - `EngineLoop`: periodic per-aggregate evaluation cycle, plan emission via RPC cast, result-listener lifecycle

### Scope resolution
The engine operates on a fixed set of aggregates defined at startup via
`[engine] aggregates` and `[engine] include_unassigned_hosts`. Each
aggregate is evaluated independently every cycle. The unassigned-hosts
pool is resolved by `NovaClient.get_hosts_in_aggregate(None)`.

### Data flow
```
oslo.config → EngineLoop
                ├── loads policies (Pydantic) - all enabled policies apply to every aggregate
                ├── resolves aggregate scope (aggregates list + unassigned pool)
                │
                ├── for each aggregate in scope:
                │   ├── hosts = NovaClient.get_hosts_in_aggregate(aggregate)
                │   │
                │   ├── for each enabled policy:
                │   │   └── PolicyScorer.evaluate(policy, hosts) → PolicyResult
                │   │       ├── PrometheusClient.instant_query(imbalance_query)
                │   │       ├── filter to scope hosts, enforce [0, 1] range
                │   │       ├── normalize, compute imbalance
                │   │       └── return PolicyResult
                │   │
                │   ├── combined_imbalance = Σ (policy.weight × policy.imbalance)
                │   │
                │   └── if any policy.imbalance_detected OR enforcer.enabled:
                │       ├── check cooldown (skip if aggregate is cooling)
                │       │
                │       ├── VmProfiler.collect(policies, hosts, host_scores_by_policy)
                │       │   ├── NovaClient.list_instances_on_host(host)
                │       │   ├── PrometheusClient.instant_query(vm_profile_query)  - one per policy
                │       │   └── VmProfile.weights[policy_name] for every policy
                │       │
                │       ├── AffinityEnforcer.enforce(...)        - phase="affinity"
                │       │
                │       ├── Planner.plan(...)                     - phase="spread" or "pack"
                │       │
                │       ├── _filter_unavailable_vms(vm_profiles)         - drop quarantined + cooling VMs
                │       │
                │       └── if not dry_run and plan has steps:
                │           ├── for each step: RPCClient.cast('execute_migration', task)
                │           └── CooldownTracker.record_plan_emission(aggregate, instance_uuids)
                │
                └── log CycleReport (aggregate results, per-policy imbalances, plans)

In parallel, a notification listener per aggregate consumes results:
  kronos.results.<aggregate> → MigrationResultEndpoint.info()
                               ├── migration.completed            - DEBUG log
                               ├── migration.failed (retry pending)- DEBUG log
                               └── migration.failed (final)        - INFO log, quarantine_instance()
```

### Combined scoring

When multiple policies share an aggregate their imbalance values combine
into a single weighted score:

```
combined_imbalance = Σ (policy.weight × policy.imbalance)
```

Constraints on `PoliciesConfig` (load time):
- Enabled policy `weight` values sum to 1.0
- All policies in a file share the same `mode` (spread or pack, not both)
- Each `imbalance_query` must return values in [0, 1] (runtime, scorer-enforced)

Each VM carries per-policy weights:
```python
VmProfile.weights: dict[str, float]   # {policy_name: weight}
```
Simulating a move applies the weight subtraction and addition **per
policy** - a single move affects every policy's scores.

**Move acceptance rule**: a candidate move is rejected if it would
**worsen** any individual policy's imbalance **and** the policy's new
imbalance is above its own threshold. Lets the planner trade dimensions
(small CPU regression to fix a large memory problem) while preventing:
- pushing a previously-OK policy into violation
- making an already-violating policy even worse

**Stopping criterion**: greedy spread stops when every policy's
imbalance is at or below its threshold, or when
`max_migrations_per_cycle` is reached, or when no improving move
exists.

### Score normalization
Raw per-host Prometheus values are normalised 0.0-1.0 within each
aggregate using min-max for logging purposes. The planner uses raw
values (already in [0, 1]) for its combined score math.

### Planner algorithms

**Spread (greedy combined-score simulation)**: each round, try every
(source, vm, dest) combination, compute the new combined imbalance,
reject any move that worsens a policy past its threshold, pick the
move with the largest combined improvement. Repeat until all policies
are happy, `max_migrations_per_cycle` is hit, or no improving move
exists.

**Pack (First Fit Decreasing on combined score)**:
1. Sort hosts by combined weighted score ascending (coldest first = drain order)
2. For each drain host, sort VMs by combined weighted weight descending
3. For each VM, find the fullest non-draining host where every policy's
   projected score stays below that policy's `capacity_threshold`
4. Track draining hosts so VMs are never moved *to* them
5. Stop at `max_migrations_per_cycle`

### VM profiling
The `VmProfiler` runs once per aggregate and builds
`dict[instance_uuid → VmProfile]` where each profile carries per-policy
weights. For every (instance, policy) pair:
1. Query Prometheus with that policy's `vm_profile_query`
2. Map the label value to a Nova instance via `vm_profile_label_type`
3. If no data, apply the policy's fallback:
   - `skip` - drop the VM from the whole combined profile (default, safest)
   - `flavor_vcpu_ratio` - estimate from host score and vCPU count
   - `host_average` - assume equal share of host's score

A VM is excluded from planning entirely if any policy with `skip`
fallback has no data for it.

### Constraint checking
`ConstraintChecker` reads Nova server groups across all projects and
treats **all four** placement policies as move-blocking:
- `anti-affinity` / `soft-anti-affinity`: a move is rejected if another
  group member already lives on the destination host
- `affinity` / `soft-affinity`: a move is rejected unless every other
  *currently placed* member of the group is already on the destination
  host. Members outside the current aggregate are ignored - Kronos only
  reasons about VMs visible in the planner's `vms_by_host` index

Soft rules are currently enforced with the same strictness as hard
ones. They will later be promoted to weighted planner penalties (so a
move that mildly violates a soft rule can still win if it resolves a
much larger imbalance), but for now both flavours veto.

The cache is invalidated each engine cycle.

Future: NUMA, CPU feature flags, flavor extra specs, soft-rule
penalties in the planner -
https://docs.openstack.org/nova/latest/user/server-groups.html

### Host evacuation

`Evacuator` drains VMs off compute hosts whose nova-compute service is
administratively disabled.  Enabled per engine via:

```ini
[engine]
evacuate_disabled_hosts = false   # default
```

**Algorithm**, per aggregate:

1. Intersect the cluster-wide service map with the aggregate's host
   list (so disabled hosts in unrelated aggregates / AZs are
   ignored).
2. Identify candidate VMs: every VM on a `status=disabled` *and*
   `state=up` host in scope.  Hosts with `state=down` are not
   evacuated - live migration cannot drain them.
3. For each candidate, simulate every (vm, dest) pair and accept it
   only when:
   - `ConstraintChecker.check` passes (destination is up + enabled,
     and no server-group rule is broken).
   - The move does not push any policy past its threshold.
4. Pick the pair that leaves combined imbalance lowest, apply it,
   record a step with phase `evacuate`, and repeat.

**Order**: evacuator → affinity enforcer → imbalance planner.  All
three share one `max_migrations_per_cycle` budget; the evacuator
consumes first.

**Pre-flight (executor)**: every migration re-checks source +
destination service state.  Refuses if either is no longer up +
enabled, *except* evacuation tasks (`phase=evacuate`) tolerate a
`status=disabled` source - that is precisely the condition that
made the move legitimate.

### Affinity enforcement

`AffinityEnforcer` runs **before** the imbalance planner and proactively
repairs server-group placements that are already in violation. The
checker guards against new violations; the enforcer fixes existing ones.

Enabled per policy-type via `[engine]`:
```ini
enforce_hard_affinity = false   # affinity + anti-affinity
enforce_soft_affinity = false   # soft-affinity + soft-anti-affinity
```
Both default false. The enforcer is a no-op when neither is enabled.

**Algorithm**, per aggregate:
1. Fetch the relevant server groups (filtered by enabled flags)
2. Detect violations against current VM placement:
   - `anti-affinity` / `soft-anti-affinity`: ≥2 members share a host
   - `affinity` / `soft-affinity`: members span >1 host
3. Collect the set of offending VM UUIDs across all violations
4. Simulate every (offending_vm, destination) pair. Accept only pairs that:
   - pass `ConstraintChecker` (no other group rule broken)
   - do not push any policy's imbalance above its threshold
     (`_sim.move_hurts_any_policy`)
5. Pick the pair that leaves combined imbalance lowest. Apply it, record
   a step with phase `affinity`
6. Repeat until no violations remain, budget exhausted, or no legal
   destination exists

The enforcer returns the simulated post-enforcement `scores`,
`vms_by_host`, and remaining budget. The Planner starts from those so
the imbalance pass sees the world the executor will actually observe.

**Budget sharing**: enforcement and imbalance planning share a single
`max_migrations_per_cycle` budget (max across active policies).
Enforcement consumes first; the imbalance planner gets the remainder.

**No legal repair**: if no (vm, dest) pair clears the constraint and
threshold filters, the enforcer logs a warning and stops. The
imbalance planner still runs with the remaining budget.

**Cooldown semantics**: repair moves go through the normal plan
emission path, so aggregate and instance cooldowns fire exactly as
they do for imbalance-driven moves.

### Cooldown tracking
`CooldownTracker` prevents oscillation and migration storms:

- **Aggregate-level**: after emitting a plan for an aggregate, skip
  planning for the same aggregate until `engine.cooldown` expires.
  Global across all policies - combined scoring means one plan covers
  all policies for an aggregate per cycle.
- **Instance-level**: after including a VM in a plan, skip it for
  `engine.instance_cooldown` seconds. Prevents a VM bouncing between
  hosts via different migrations.
- **Quarantine**: after a migration definitively fails (retries
  exhausted on `PreFlightError` / `MigrationFailed` / `MigrationTimeout`),
  skip the VM for `engine.instance_quarantine_seconds`. Use `-1` for
  indefinite quarantine. `NovaClientError` is treated as transient and
  does not quarantine; the normal instance cooldown governs re-planning.

`EngineLoop._filter_unavailable_vms` is the single chokepoint that
enforces both instance cooldown and quarantine: it runs immediately
after `VmProfiler.collect()` and drops excluded UUIDs from the
`vm_profiles` dict before the enforcer and planner see it. The scorer
still computes per-host imbalance from fresh Prometheus data - only
the *candidate set for movement* is filtered.

### Result listener

`MigrationResultEndpoint` (`result_listener.py`) subscribes to
`kronos.results.<aggregate>` and reacts per event type:

| Event | Condition | Action |
|---|---|---|
| `migration.completed` | - | DEBUG log, no state change |
| `migration.failed` | `retry_count < max_retries` (retry pending) | DEBUG log, no state change |
| `migration.failed` | final attempt, `error_type` in `{PreFlightError, MigrationFailed, MigrationTimeout}` | INFO log, `quarantine_instance(uuid, instance_quarantine_seconds)` |
| `migration.failed` | final attempt, `error_type == NovaClientError` | INFO log; no quarantine (transient infra error) |
| `migration.failed` | final attempt, unknown `error_type` | INFO log, defensive quarantine |

`EngineLoop._init_messaging` creates one notification listener per
aggregate (plus one for the unassigned pool) using
`get_notification_listener()`. Listeners are started before the loop
begins and drained in `_shutdown_messaging()` on SIGTERM/SIGINT.

Listeners are only wired when `dry_run=false`. In dry-run, no plans
are cast so no results ever arrive.

### Replay cooldown seeding

`kronos-replay` can seed the tracker from `<snapshot>/cooldowns.json`
so offline runs reproduce scenarios where specific VMs are already
cooling or quarantined. The file is optional and has three optional
sections:

```json
{
  "aggregate_cooldowns":  {"gpu": 120},
  "instance_cooldowns":   {"vm-abc": 300},
  "instance_quarantines": {"vm-xyz": 1800, "vm-banned": -1}
}
```

Values are seconds remaining at replay time. `-1` in
`instance_quarantines` means indefinite. `kronos-record` writes an
empty template; operators edit it to stage test scenarios.

Seeding uses `CooldownTracker.seed_aggregate_cooldown`,
`seed_instance_cooldown`, and `seed_instance_quarantine`. Aggregate
and instance cooldowns are clamped to the configured durations so a
snapshot claiming more seconds remaining than `[engine] cooldown` /
`instance_cooldown` degrades to the configured max rather than
erroring.

### EngineLoop lifecycle
1. Load oslo.config + policies YAML
2. Resolve aggregate scope from `[engine] aggregates` and `include_unassigned_hosts`
3. Initialise Prometheus client, Nova client, scorer, profiler, constraints, evacuator, enforcer, planner, cooldown
4. If `dry_run=false`: create RPC transport + per-aggregate RPC clients, and a
   notification transport + per-aggregate result listeners
5. Enter loop. Each cycle calls `run_once(policies, aggregates)`:
   - `invalidate_cache()` on the constraint checker.
   - Fetch nova-compute services once cluster-wide; install on the
     constraint checker (host-availability gate) and pass to the
     evacuator.  On failure, install an empty map - the gate fails
     closed and no migrations are emitted that cycle.
   - For each aggregate -> score -> cooldown check -> profile -> drop
     VMs whose source host is `state=down` -> filter
     quarantined/cooling VMs -> evacuate disabled hosts -> enforce
     affinity -> plan imbalance -> cast -> log.
6. Handle SIGTERM/SIGINT for graceful shutdown (drains result listeners)

### Dependency injection and `run_once()`

`EngineLoop.__init__` accepts optional `nova`, `prometheus`,
`cooldown`, and `timings` keyword arguments.  Production callers
(`kronos-engine`) leave them at `None` so the loop builds the live
clients from `conf`.  Offline callers - `kronos-replay`, unit tests -
pass their own stubs to skip the network.

`run_once(*, policies=None, aggregates=None) -> CycleReport` is the
public single-cycle entry point.  It loads policies from disk and
resolves aggregates from conf when not provided, then delegates to
`_run_cycle`.  It does NOT initialise messaging, install signal
handlers, validate the snapshot directory, or sleep between cycles -
those concerns belong to `start()`, which now calls `run_once()` once
per interval.

`self.timings: dict[str, float] | None` is an opt-in per-phase
wall-clock accumulator.  When set (typically to `{}` by the caller),
`_evaluate_aggregate` adds seconds spent in the scorer, profiler,
evacuator, enforcer, and planner phases to it on every cycle.
Production leaves it as `None` and pays no timer cost.  `kronos-replay
--time` populates it and prints the totals after `run_once()`.

---

## kronos/executor - Migration runner

Consumes migration tasks via RPC from the engine and executes them via
the Nova live-migrate API. The executor never decides *what* to migrate
- it only validates, executes, and reports results.

### Key files
- `worker.py` - `ExecutorWorker`: top-level wiring (RPC server, scheduler, runner, result notifier); `MigrationRPCEndpoint`: RPC endpoint exposing `execute_migration`
- `scheduler.py` - `TaskScheduler`: priority queue sorted by `not_before`, semaphore for concurrency control
- `migrate.py` - `MigrationRunner`: pre-flight check, Nova live-migrate, poll status, post-flight verify

### Deployment model
- One executor per aggregate (or for the unassigned-hosts pool)
- Started with either `--aggregate <name>` or `--unassigned`
- RPC topic: `kronos.migrations.<aggregate>` (or `kronos.migrations._unassigned_` for the unassigned pool)
- Results topic: `kronos.results.<aggregate>`

### Messaging
Two oslo.messaging primitives, each for a reason:
- **Engine → Executor**: RPC cast. Exactly-one delivery via competing
  consumers.
- **Executor → Engine**: Notifications. The engine consumes
  `migration.completed` / `migration.failed` events to drive cooldown
  and quarantine state. On failure the payload carries `error_type`
  (exception class name) so the engine can distinguish VM-specific
  failures (`PreFlightError`, `MigrationFailed`, `MigrationTimeout`)
  from transient infrastructure errors (`NovaClientError`).

### Message flow
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
Engine notification listener updates cooldown/quarantine state
```

### Retry logic
- On failure, if `retry_count < max_retries`, the task is **re-cast**
  to the same RPC migrations topic with incremented `retry_count` and
  exponential backoff `not_before`
- After `max_retries`, the task is logged and dropped (dead letter)
- Retries go back through RabbitMQ

### Restart behaviour
- oslo.messaging acks messages when the handler returns (submission to
  the local scheduler), not when the migration completes
- If the executor restarts, tasks in the local scheduler queue are lost
- Tasks mid-migration: Nova continues the migration regardless; the
  engine re-evaluates next cycle and sees updated host scores
- Acceptable because the system is self-healing - the engine re-plans
  every `evaluation_interval` seconds

### Safety guarantees
1. Pre-flight check before every migration (the plan may be stale)
2. Staggered execution via `not_before` timestamps on each task
3. Concurrency cap via semaphore (`max_concurrent_migrations`)
4. Hard timeout on migration polling
5. Post-flight verification (host + status)
6. Idempotent: pre-flight catches duplicate or stale tasks

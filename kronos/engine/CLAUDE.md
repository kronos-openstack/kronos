# Engine Module

## Purpose
Main control loop, policy evaluation, VM profiling, constraint checking,
combined-scoring migration planning, and cooldown tracking. The engine is
a pure planner — it evaluates policies, detects imbalance, simulates
moves, produces migration plans, and casts them over RPC to the executor.
It never calls Nova live-migrate directly.

## Key Files
- `types.py` — Dataclasses and enums: HostScore, VmProfile, MigrationStep, MigrationPlan, MigrationTask, MigrationResult, MigrationPhase, PolicyResult, AggregateResult, CycleReport
- `_sim.py` — Shared simulation helpers (combined_imbalance, simulate_move, etc.) used by both the planner and the affinity enforcer
- `scorer.py` — PolicyScorer: runs one policy's PromQL imbalance_query against a host list, normalises scores, enforces the [0, 1] contract
- `profiler.py` — VmProfiler: collects per-VM resource weights across all policies for an aggregate in one pass
- `planner.py` — Planner: combined-scoring simulation (spread greedy + pack First Fit Decreasing)
- `constraints.py` — ConstraintChecker: validates moves against all four Nova server group placement policies (affinity, anti-affinity, soft-affinity, soft-anti-affinity)
- `affinity_enforcer.py` — AffinityEnforcer: repair pass that moves VMs out of existing server-group violations, using the same combined-imbalance math to pick destinations
- `cooldown.py` — CooldownTracker: aggregate-level and instance-level cooldowns
- `loop.py` — EngineLoop: periodic per-aggregate evaluation cycle, plan emission via RPC cast

## Scope Resolution
The engine operates on a fixed set of aggregates defined at startup via
`[engine] aggregates` and `[engine] include_unassigned_hosts`. Each
aggregate is evaluated independently every cycle. The unassigned-hosts
pool is resolved by calling `NovaClient.get_hosts_in_aggregate(None)`.

## Data Flow
```
oslo.config → EngineLoop
                ├── loads policies (Pydantic) — all enabled policies apply to every aggregate
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
                │       │   ├── PrometheusClient.instant_query(vm_profile_query)  — one per policy
                │       │   └── VmProfile.weights[policy_name] for every policy
                │       │
                │       ├── AffinityEnforcer.enforce(...)        — phase="affinity"
                │       │   ├── detect server-group violations (hard or soft, per config)
                │       │   ├── for each violation, pick best (offending_vm, dest) pair
                │       │   │     destination minimises combined imbalance
                │       │   │     destination cannot push any policy above threshold
                │       │   │     destination cannot break another server-group rule
                │       │   └── consumes enforcement moves from max_migrations_per_cycle
                │       │
                │       ├── Planner.plan(...)                     — phase="spread" or "pack"
                │       │   ├── starts from post-enforcement scores + vms_by_host
                │       │   ├── budget = max_migrations_per_cycle − enforcement steps
                │       │   ├── ConstraintChecker (affinity, anti-affinity, soft variants)
                │       │   ├── spread: greedy best-move-per-round minimising combined imbalance
                │       │   └── pack: First Fit Decreasing, combined utilization for ordering
                │       │
                │       └── if not dry_run and plan has steps:
                │           ├── for each step: RPCClient.cast('execute_migration', task)
                │           └── CooldownTracker.record_plan_emission(aggregate, instance_uuids)
                │
                └── log CycleReport (aggregate results, per-policy imbalances, plans)
```

## Combined Scoring

When multiple policies share the same aggregate, their imbalance values
are combined into a single weighted score:

```
combined_imbalance = Σ (policy.weight × policy.imbalance)
```

Constraints on the PoliciesConfig (enforced at load time):
- Enabled policy `weight` values sum to 1.0
- All policies in a file share the same `mode` (spread or pack, not both)
- Each `imbalance_query` must return values in [0, 1] — enforced at
  runtime by the scorer; a policy returning out-of-range values is
  skipped for that cycle

### Simulation in the planner

Each VM carries per-policy weights:

```python
VmProfile.weights: dict[str, float]   # {policy_name: weight}
```

When simulating a move, the planner applies the weight subtraction and
addition **per policy** — a single move affects every policy's scores.

### Move acceptance rule

A candidate move is rejected if it would **worsen** any individual
policy's imbalance **and** the policy's new imbalance is above its own
threshold. This allows the planner to make trade-offs between dimensions
(e.g. a small CPU regression to fix a large memory problem) while
preventing:

- pushing a previously-OK policy into violation
- making an already-violating policy even worse

### Stopping criterion

Greedy spread stops when every individual policy's imbalance is at or
below its threshold, or when `max_migrations_per_cycle` is reached, or
when no improving move exists.

## Score Normalization
Raw per-host Prometheus values are normalised 0.0-1.0 within each
aggregate using min-max for logging purposes. The planner uses raw
values (already in [0, 1]) for its combined score math.

## Planner Algorithms

### Spread (greedy combined-score simulation)
Each round: try every (source, vm, dest) combination, compute the new
combined imbalance, reject any move that worsens a policy past its
threshold, pick the move with the largest combined improvement. Repeat
until all policies are happy, `max_migrations_per_cycle` is hit, or no
improving move exists.

### Pack (First Fit Decreasing on combined score)
1. Sort hosts by combined weighted score ascending (coldest first = drain order)
2. For each drain host, sort its VMs by combined weighted weight descending (biggest first)
3. For each VM, find the fullest non-draining host where every policy's
   projected score stays below that policy's `capacity_threshold`
4. Track draining hosts so VMs are never moved *to* them
5. Stop at `max_migrations_per_cycle`

## VM Profiling
The VmProfiler runs once per aggregate and builds a
`dict[instance_uuid → VmProfile]` where each profile carries per-policy
weights. For every instance × policy pair:

1. Query Prometheus with that policy's `vm_profile_query`
2. Map the label value to a Nova instance via `vm_profile_label_type`
3. If the query has no data for that instance, apply the policy's fallback:
   - `skip` — drop the VM from the whole combined profile (safest, default)
   - `flavor_vcpu_ratio` — estimate from host score and vCPU count
   - `host_average` — assume equal share of host's score

A VM is excluded from planning entirely if any policy with `skip`
fallback has no data for it.

## Constraint Checking
The ConstraintChecker reads Nova server groups across all projects and
treats **all four** Nova server group placement policies as
move-blocking:

- `anti-affinity` / `soft-anti-affinity`: a move is rejected if another
  group member already lives on the destination host.
- `affinity` / `soft-affinity`: a move is rejected unless every other
  *currently placed* member of the group is already on the destination
  host. Members outside the current aggregate are ignored — Kronos
  only reasons about VMs visible in the planner's `vms_by_host` index.

Soft rules are currently enforced with the same strictness as hard
ones. They will later be promoted to weighted planner penalties (so a
move that mildly violates a soft rule can still win if it resolves a
much larger imbalance), but for now both flavours veto.

The cache is invalidated each engine cycle.

Guarding against broken rules is separate from *repairing* existing
violations — see the **Affinity Enforcement** section below.

Future: NUMA, CPU feature flags, flavor extra specs, soft-rule
penalties in the planner —
https://docs.openstack.org/nova/latest/user/server-groups.html

## Affinity Enforcement

The AffinityEnforcer runs **before** the imbalance planner and
proactively repairs server-group placements that are already in
violation. It complements the ConstraintChecker: the checker guards
against new violations, the enforcer fixes existing ones.

Enabled per policy-type via `[engine]`:

```ini
enforce_hard_affinity = false   # affinity + anti-affinity
enforce_soft_affinity = false   # soft-affinity + soft-anti-affinity
```

Both default false. The enforcer is a no-op when neither is enabled.

### Algorithm

For each aggregate, the enforcer iterates:

1. Fetch the relevant server groups (filtered by the enabled flags).
2. Detect violations against the current VM placement:
   - `anti-affinity` / `soft-anti-affinity`: ≥2 members share a host.
   - `affinity` / `soft-affinity`: members span >1 host.
3. Collect the set of offending VM UUIDs across all violations.
4. Simulate every (offending_vm, destination) pair. Accept only pairs
   that:
   - pass the ConstraintChecker (no other group rule broken);
   - do not push any policy's imbalance above its threshold
     (`_sim.move_hurts_any_policy`).
5. Pick the pair that leaves the combined imbalance lowest. Apply it
   to the simulation, record a step with phase `affinity`.
6. Repeat until no violations remain, budget exhausted, or no legal
   destination exists.

The enforcer returns the simulated post-enforcement `scores`,
`vms_by_host`, and remaining budget. The Planner starts from those so
the imbalance pass sees the world the executor will actually observe.

### Budget sharing

Enforcement and imbalance planning share a single
`max_migrations_per_cycle` budget (max across active policies).
Enforcement consumes first; the imbalance planner gets the
remainder. This keeps total churn bounded regardless of whether
enforcement is active.

### When no legal repair exists

If no (vm, dest) pair clears the ConstraintChecker and threshold
filters, the enforcer logs a warning and stops. The imbalance planner
still runs with the remaining budget. "We cannot do magic" —
unrepairable violations are a human-attention problem, not a
blocker.

### Cooldown semantics

Repair moves go through the normal plan emission path, so aggregate
and instance cooldowns fire exactly as they do for imbalance-driven
moves.

## Cooldown Tracking
The CooldownTracker prevents oscillation and migration storms:

- **Aggregate-level**: after emitting a plan for an aggregate, skip
  planning for the same aggregate until `engine.cooldown` expires.
  Global across all policies — combined scoring means one plan covers
  all policies for an aggregate per cycle.
- **Instance-level**: after including a VM in a plan, skip it for
  `engine.instance_cooldown` seconds. Prevents a VM bouncing between
  hosts via different migrations.

Active and passive engines both maintain a tracker. The passive engine
listens on `kronos.results.<aggregate>` and updates its own cooldown
state from `migration.completed` / `migration.failed` notifications, so
it has warm state on failover.

## EngineLoop Lifecycle
1. Load oslo.config + policies YAML
2. Resolve aggregate scope from `[engine] aggregates` and `include_unassigned_hosts`
3. Initialise Prometheus client, Nova client, scorer, profiler, constraints, planner, cooldown
4. If `dry_run=false`: create RPC transport and per-aggregate RPC clients
5. Enter loop: for each aggregate → score → cooldown check → profile → plan → cast → log
6. Handle SIGTERM/SIGINT for graceful shutdown
7. Constraint cache invalidated each cycle

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

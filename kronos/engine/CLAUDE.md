# Engine Module

## Purpose
Main control loop, policy evaluation, VM profiling, constraint checking,
and migration planning. The engine is a pure planner — it evaluates
policies, detects imbalance, collects VM profiles, simulates moves,
and produces migration plans. It never directly calls Nova migrate.

## Key Files
- `types.py` — Dataclasses: HostScore, VmProfile, MigrationStep, MigrationPlan, MigrationTask, MigrationResult, PolicyResult, CycleReport
- `scorer.py` — PolicyScorer: evaluates PromQL queries, produces per-host scores
- `profiler.py` — VmProfiler: collects per-VM resource weights from Prometheus + Nova
- `planner.py` — Planner: simulation-based migration planning (spread + pack)
- `constraints.py` — ConstraintChecker: validates moves against server group anti-affinity
- `cooldown.py` — CooldownTracker: prevents oscillation via policy-level and instance-level cooldowns
- `loop.py` — EngineLoop: periodic evaluation cycle, plan emission via oslo.messaging

## Data Flow
```
oslo.config → EngineLoop
                ├── loads policies (Pydantic)
                ├── for each enabled policy:
                │   ├── PolicyScorer.evaluate(policy)
                │   │   ├── NovaClient.get_aggregate_hosts(aggregate)
                │   │   ├── PrometheusClient.instant_query(imbalance_query)
                │   │   ├── normalize scores
                │   │   └── return PolicyResult
                │   │
                │   └── if imbalance detected:
                │       ├── VmProfiler.collect(policy, hosts, scores)
                │       │   ├── NovaClient.list_instances_on_host(host)
                │       │   ├── PrometheusClient.instant_query(vm_profile_query)
                │       │   └── apply fallback for missing VMs
                │       │
                │       └── Planner.plan(policy, host_scores, vm_profiles)
                │           ├── ConstraintChecker (anti-affinity)
                │           ├── spread: greedy best-move-per-round
                │           └── pack: First Fit Decreasing, coldest hosts first
                │
                │
                ├── if dry_run=true: log only
                └── if dry_run=false: emit MigrationTasks to kronos.migrations.<agg>
                    └── CooldownTracker.record_plan_emission()
```

## Score Normalization
Raw Prometheus values are normalized 0.0-1.0 within each aggregate using min-max.
Imbalance is detected when: `(max_score - min_score) > threshold`

## Planner Algorithms

### Spread (greedy simulation)
Each round: try every (source, vm, dest) combination, pick the single
move that reduces imbalance the most. Apply it to simulated state, repeat
up to `max_migrations_per_cycle`. Stop early if imbalance drops below threshold.

### Pack (First Fit Decreasing)
Goal: consolidate VMs into the fewest hosts possible.
1. Sort hosts coldest-first (drain order).
2. For each host, sort its VMs biggest-first.
3. For each VM, find the fullest non-draining host where
   `score + vm.weight <= capacity_threshold`.
4. Track draining hosts so VMs are never moved *to* them.
5. Stop at `max_migrations_per_cycle`.

## VM Profiling
The VmProfiler queries `vm_profile_query` from Prometheus and maps
labels back to Nova instances via `vm_profile_label_type`. Three
fallback strategies for VMs without Prometheus data:
- `skip` — exclude from planning (safest)
- `flavor_vcpu_ratio` — estimate from host score and vCPU count
- `host_average` — assume equal share of host's score

## Constraint Checking
The ConstraintChecker validates proposed moves against Nova server
group anti-affinity rules. The cache is invalidated each engine cycle.
Future: NUMA, CPU feature flags, flavor extra specs, consider other server groups policy in openstack https://docs.openstack.org/nova/2024.1/user/server-groups.html

## Cooldown Tracking
The CooldownTracker prevents oscillation and migration storms:
- **Policy-level**: after emitting a plan, skip planning for that policy until `cooldown` expires
- **Instance-level**: after including a VM in a plan, skip it for `instance_cooldown` seconds
- Both active and passive engines listen on `kronos.results.<aggregate>` to maintain warm cooldown state
- On failover, passive engine already knows recent migrations — no cold-start re-planning storm

## Future Extension Points
- `loop.py` will acquire tooz lock before entering loop (M4)

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

## EngineLoop Lifecycle
1. Load oslo.config + policies YAML
2. Initialize clients, profiler, constraints, planner, cooldown tracker
3. If dry_run=false: initialize oslo.messaging transport + per-aggregate notifiers
4. Enter loop: score → cooldown check → profile → plan → emit/log → sleep
5. Handle SIGTERM/SIGINT for graceful shutdown
6. Constraint caches invalidated each cycle
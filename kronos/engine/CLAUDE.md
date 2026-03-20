# Engine Module

## Purpose
Main control loop, policy evaluation, VM profiling, constraint checking,
and migration planning. The engine is a pure planner — it evaluates
policies, detects imbalance, collects VM profiles, simulates moves,
and produces migration plans. It never directly calls Nova migrate.

## Key Files
- `types.py` — Dataclasses: HostScore, VmProfile, MigrationStep, MigrationPlan, PolicyResult, CycleReport
- `scorer.py` — PolicyScorer: evaluates PromQL queries, produces per-host scores
- `profiler.py` — VmProfiler: collects per-VM resource weights from Prometheus + Nova
- `planner.py` — Planner: simulation-based migration planning (spread + pack)
- `constraints.py` — ConstraintChecker: validates moves against server group anti-affinity
- `loop.py` — EngineLoop: periodic evaluation cycle

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
                └── log CycleReport + MigrationPlans
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

## Future Extension Points
- `loop.py` will publish MigrationPlan to oslo.messaging queue (M3)
- `loop.py` will acquire tooz lock before entering loop (M4)

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

## EngineLoop Lifecycle
1. Load oslo.config + policies YAML
2. Initialize clients, profiler, constraints, planner
3. Enter loop: score → profile → plan → log → sleep(evaluation_interval)
4. Handle SIGTERM/SIGINT for graceful shutdown
5. Constraint caches invalidated each cycle
# Engine Module

## Purpose
Main control loop and policy evaluation logic. The engine is a pure planner —
it evaluates policies, detects imbalance, and (in M2+) generates migration plans.
It never directly calls Nova migrate.

## Key Files
- `types.py` — Dataclasses: HostScore, PolicyResult, CycleReport
- `scorer.py` — PolicyScorer: evaluates PromQL queries, produces per-host scores
- `loop.py` — EngineLoop: periodic evaluation cycle

## M1 Scope (dry-run only)
- Evaluate all enabled policies on each cycle
- Query Prometheus for host-level metrics
- Match Prometheus results to Nova hosts in the policy's aggregate
- Normalize scores, detect imbalance
- Log results as CycleReport
- NO migration planning (M2), NO queue publishing (M3), NO HA (M4)

## Score Normalization
Raw Prometheus values are normalized 0.0-1.0 within each aggregate using min-max.
Imbalance is detected when: `(max_score - min_score) > threshold`

## Data Flow
```
oslo.config → EngineLoop
                ├── loads policies (Pydantic)
                ├── for each enabled policy:
                │   └── PolicyScorer.evaluate(policy)
                │       ├── NovaClient.get_aggregate_hosts(aggregate)
                │       ├── PrometheusClient.instant_query(imbalance_query, expected=hosts)
                │       ├── normalize scores
                │       └── return PolicyResult
                └── log CycleReport
```

## Future Extension Points
- `scorer.py` will feed into a Planner class (M2) that decides which VMs to move
- `loop.py` will publish MigrationPlan to oslo.messaging queue (M3)
- `loop.py` will acquire tooz lock before entering loop (M4)
- Policy modes ("spread"/"pack") will become Stevedore plugins (M2+)

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

## EngineLoop Lifecycle
1. Load oslo.config + policies YAML
2. Initialize Prometheus and Nova clients
3. Enter loop: evaluate → log → sleep(evaluation_interval)
4. Handle SIGTERM/SIGINT for graceful shutdown
5. Each cycle is independent — no state carried between cycles in M1

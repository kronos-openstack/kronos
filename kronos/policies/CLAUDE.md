# Policies Module

## Purpose
Load and validate policy definition YAML files using Pydantic v2 models.
Policies define the PromQL-driven scheduling rules that the engine evaluates.

## Why Pydantic (not oslo.config) for policies
Policies contain PromQL query strings, mode-specific fields (capacity_query
for pack mode), and cross-field validation (pack requires capacity_query,
weight sum, single mode). This is richer than oslo.config's flat key-value
model. oslo.config handles daemon config; Pydantic handles the policy DSL.

## Key Files
- `models.py` — Pydantic v2 models: PolicyConfig, PoliciesConfig
- `loader.py` — YAML file loading, returns validated PoliciesConfig

## Policy Config Fields
- `name`: unique identifier (lowercase, alphanumeric + hyphens/underscores)
- `mode`: `spread` (balance load) or `pack` (consolidate)
- `weight`: 0.0-1.0, contribution to the combined aggregate imbalance score
- `imbalance_query`: PromQL returning a per-host utilization ratio in [0, 1]
- `host_label`: Prometheus label in `imbalance_query` that identifies each host (default `host`)
- `vm_profile_query`: PromQL returning a per-VM metric for simulation-based planning
- `vm_profile_label` / `vm_profile_label_type`: how to map Prometheus labels to Nova instances
- `vm_profile_fallback`: what to do when a VM has no Prometheus data (`skip`, `flavor_vcpu_ratio`, `host_average`)
- `threshold`: imbalance threshold (max - min) to trigger rebalancing
- `capacity_query`: PromQL for pack mode capacity check (required for pack mode)
- `capacity_threshold`: max host utilization ceiling for pack mode (no host goes above this)
- `max_migrations_per_cycle`: cap per evaluation cycle
- `enabled`: whether this policy is active

## Aggregates are NOT in the policy
Aggregates are configured at engine level via `[engine] aggregates` and
`[engine] include_unassigned_hosts` in `kronos.conf`. A policies.yaml is
bound to the engine that loads it, and applies to every aggregate in
that engine's scope.

## PoliciesConfig Validators
`PoliciesConfig` enforces cross-policy invariants on load:

- **Unique names**: no two policies share a `name`
- **Single mode**: all policies in one file share a `mode` (spread and
  pack are opposite intents and cannot be combined into one score)
- **Weight sum**: enabled policy `weight` values sum to 1.0 exactly
  (with float tolerance 1e-6). Disabled policies are excluded from
  the sum

## The [0, 1] Contract
`imbalance_query` must return values in the [0, 1] range (utilization
ratios). This is a runtime contract enforced by the scorer: a policy
whose query returns out-of-range values is skipped for that cycle and
logs an error. Not enforced at load time because the query must run
against live Prometheus to check.

## Adding New Policy Fields
1. Add field to `PolicyConfig` in `models.py` with Pydantic `Field()`
2. Add cross-field validation via `@model_validator` if needed
3. Update the sample `policies.yaml` in `internal-documentation/`
4. Add tests in `tests/unit/policies/test_models.py`
5. Update this CLAUDE.md

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

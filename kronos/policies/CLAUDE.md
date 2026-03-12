# Policies Module

## Purpose
Load and validate policy definition YAML files using Pydantic v2 models.
Policies define the PromQL-driven scheduling rules that the engine evaluates.

## Why Pydantic (not oslo.config) for policies
Policies contain PromQL query strings, per-aggregate tuning, mode-specific fields
(capacity_query for bin_pack), and cross-field validation (drain < capacity threshold).
This is richer than oslo.config's flat key-value model. oslo.config handles daemon config;
Pydantic handles the policy DSL.

## Key Files
- `models.py` — Pydantic v2 models: PolicyConfig, PoliciesConfig
- `loader.py` — YAML file loading, returns validated PoliciesConfig

## Policy Config Fields
- `name`: unique identifier (lowercase, alphanumeric + hyphens)
- `mode`: "spread" (balance load) or "pack" (consolidate)
- `aggregate`: Nova host aggregate name (migrations stay within)
- `weight`: 0.0-1.0, relative importance when combining policies
- `imbalance_query`: PromQL returning per-host metric (label: host)
- `vm_profile_query`: PromQL returning per-VM metric
- `vm_profile_label` / `vm_profile_label_type`: how to map Prometheus labels to Nova instances
- `vm_profile_fallback`: what to do when VM has no Prometheus data ("skip", "flavor_vcpu_ratio", "host_average")
- `threshold`: imbalance threshold to trigger rebalancing
- `cooldown`: minimum time between migrations for this policy (parsed from "10m", "1h", etc.)
- `capacity_threshold`: max utilization for bin_pack destinations
- `max_migrations_per_cycle`: cap per evaluation cycle

## Adding New Policy Fields
1. Add field to PolicyConfig in models.py with Pydantic Field()
2. Add cross-field validation if needed (@model_validator)
3. Update policies.yaml.sample in etc/kronos/
4. Add tests in tests/unit/policies/test_models.py
5. Update this CLAUDE.md

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

## Duration Parsing
Cooldown and similar fields accept: "10m", "1h", "300" (seconds), "1h30m".
Parsed by `pytimeparse2.parse()` via custom field_validator into timedelta.

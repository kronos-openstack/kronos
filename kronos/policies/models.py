"""Pydantic v2 models for Kronos policy YAML configuration.

All policies in a file apply to every aggregate the engine manages.
Aggregates are defined at engine level via ``[engine] aggregates`` and
``[engine] include_unassigned_hosts`` in ``kronos.conf``.

Combined scoring: each policy contributes to a weighted sum used as the
aggregate's imbalance score.  This requires:

* All policies share the same ``mode`` (spread and pack are opposite
  intents and cannot be combined).
* Enabled policy ``weight`` values sum to 1.0.
* Each ``imbalance_query`` returns values in [0, 1] - enforced at runtime
  by the scorer.
"""

from __future__ import annotations

import enum
import math
from typing import Self

from pydantic import BaseModel, Field, model_validator


class PolicyMode(enum.StrEnum):
    """Scheduling mode for a policy."""

    SPREAD = "spread"
    PACK = "pack"


class VmProfileLabelType(enum.StrEnum):
    """How to map Prometheus labels back to Nova instance identifiers."""

    NOVA_INTERNAL_NAME = "nova_internal_name"
    NOVA_INSTANCE_UUID = "nova_instance_uuid"
    NOVA_DISPLAY_NAME = "nova_display_name"


class VmProfileFallback(enum.StrEnum):
    """Strategy when a VM has no Prometheus profile data."""

    SKIP = "skip"
    FLAVOR_VCPU_RATIO = "flavor_vcpu_ratio"
    HOST_AVERAGE = "host_average"


class PolicyConfig(BaseModel):
    """Single scheduling policy definition."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Unique policy identifier (lowercase, alphanumeric, hyphens, underscores).",
    )
    mode: PolicyMode = Field(
        ...,
        description="Scheduling mode: 'spread' balances load, 'pack' consolidates.",
    )
    weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Relative weight when combining policies into the aggregate score. "
            "Enabled policy weights in one file must sum to 1.0."
        ),
    )
    imbalance_query: str = Field(
        ...,
        min_length=1,
        description=(
            "PromQL query returning a per-host utilization ratio in [0, 1]. "
            "Out-of-range values cause the policy to be skipped at runtime."
        ),
    )
    vm_profile_query: str | None = Field(
        default=None,
        description="PromQL query returning a per-VM metric for simulation-based planning.",
    )
    vm_profile_label: str = Field(
        default="instance_name",
        description="Prometheus label in vm_profile_query that identifies each VM.",
    )
    vm_profile_label_type: VmProfileLabelType = Field(
        default=VmProfileLabelType.NOVA_INTERNAL_NAME,
        description="How to map vm_profile_label values to Nova instance identifiers.",
    )
    vm_profile_fallback: VmProfileFallback = Field(
        default=VmProfileFallback.SKIP,
        description="What to do when a VM has no Prometheus profile data.",
    )
    host_label: str = Field(
        default="host",
        description="Prometheus label in imbalance_query that identifies each host.",
    )
    threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Imbalance threshold: (max - min) must exceed this to trigger action.",
    )
    capacity_query: str | None = Field(
        default=None,
        description="PromQL query for capacity check (required for 'pack' mode).",
    )
    capacity_threshold: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description="Maximum host utilization ceiling for 'pack' mode destinations.",
    )
    max_migrations_per_cycle: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Maximum migrations the planner may propose per evaluation cycle.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether this policy is active.",
    )

    @model_validator(mode="after")
    def _pack_requires_capacity_query(self) -> Self:
        if self.mode == PolicyMode.PACK and not self.capacity_query:
            raise ValueError("'pack' mode requires a capacity_query.")
        return self


class PoliciesConfig(BaseModel):
    """Root model for the policies YAML file."""

    policies: list[PolicyConfig] = Field(
        ...,
        min_length=1,
        description="List of scheduling policy definitions.",
    )

    @model_validator(mode="after")
    def _unique_policy_names(self) -> Self:
        names = [p.name for p in self.policies]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"Duplicate policy names: {', '.join(sorted(duplicates))}"
            )
        return self

    @model_validator(mode="after")
    def _single_mode(self) -> Self:
        """All policies in one file must share a mode.

        Spread and pack are opposite intents (balance vs consolidate).
        Deploy a separate engine with its own policies.yaml for the other mode.
        """
        modes = {p.mode for p in self.policies}
        if len(modes) > 1:
            sorted_modes = ", ".join(sorted(m.value for m in modes))
            raise ValueError(
                f"All policies in one file must share a mode; found: {sorted_modes}"
            )
        return self

    @model_validator(mode="after")
    def _enabled_weights_sum_to_one(self) -> Self:
        """Enabled policies must have weights summing to 1.0."""
        enabled = [p for p in self.policies if p.enabled]
        if not enabled:
            return self
        total = sum(p.weight for p in enabled)
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(
                f"Enabled policy weights must sum to 1.0; got {total:.6f}"
            )
        return self

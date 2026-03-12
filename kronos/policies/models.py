"""Pydantic v2 models for Kronos policy YAML configuration."""

from __future__ import annotations

import enum
from datetime import timedelta
from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from pytimeparse2 import parse as _pytimeparse


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Parse a duration value into a timedelta.

    Accepts:
        - timedelta objects (passthrough)
        - int/float (interpreted as seconds)
        - strings: "10m", "1h", "1h30m", "1 hour 30 minutes", etc.
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)

    seconds = _pytimeparse(value)
    if seconds is None:
        raise ValueError(f"Cannot parse duration string: {value!r}")
    return timedelta(seconds=seconds)


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
    aggregate: str = Field(
        ...,
        min_length=1,
        description="Nova host aggregate name. Migrations stay within this boundary.",
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        le=10.0,
        description="Relative weight when combining multiple policies for the same aggregate.",
    )
    imbalance_query: str = Field(
        ...,
        min_length=1,
        description="PromQL query returning a per-host metric. Must produce a 'host' label.",
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
    cooldown: timedelta = Field(
        default=timedelta(minutes=10),
        description="Minimum time between migrations for this policy.",
    )
    capacity_query: str | None = Field(
        default=None,
        description="PromQL query for capacity check (required for 'pack' mode).",
    )
    capacity_threshold: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description="Maximum utilization for bin-pack destination hosts.",
    )
    bin_pack_drain_threshold: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Hosts below this utilization are drain candidates in 'pack' mode.",
    )
    max_migrations_per_cycle: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Maximum migrations the planner may propose per evaluation cycle.",
    )
    min_sustained_minutes: int = Field(
        default=0,
        ge=0,
        description="Imbalance must persist for this many consecutive evaluations before action.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether this policy is active.",
    )

    @field_validator("cooldown", mode="before")
    @classmethod
    def _parse_cooldown(cls, v: Any) -> timedelta:
        return parse_duration(v)

    @model_validator(mode="after")
    def _pack_requires_capacity_query(self) -> Self:
        if self.mode == PolicyMode.PACK and not self.capacity_query:
            raise ValueError("'pack' mode requires a capacity_query.")
        return self

    @model_validator(mode="after")
    def _drain_below_capacity(self) -> Self:
        if self.bin_pack_drain_threshold >= self.capacity_threshold:
            raise ValueError(
                f"bin_pack_drain_threshold ({self.bin_pack_drain_threshold}) "
                f"must be lower than capacity_threshold ({self.capacity_threshold})."
            )
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

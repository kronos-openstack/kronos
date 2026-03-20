"""Core data types for the Kronos engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kronos.policies.models import PolicyMode


@dataclass
class HostScore:
    """Score for a single host from a policy evaluation."""

    host: str
    raw_score: float
    normalized_score: float


@dataclass
class VmProfile:
    """Resource profile for a single VM.

    The ``weight`` is the single number the planner uses for simulation.
    It is dimension-agnostic: could represent CPU utilisation, memory
    pressure, or any metric the policy's ``vm_profile_query`` returns.
    When no Prometheus data is available, the fallback strategy fills
    this value from Nova flavor data or host averages.
    """

    instance_uuid: str
    instance_name: str
    host: str
    weight: float
    source: str = "prometheus"


@dataclass
class MigrationStep:
    """A single proposed VM migration within a plan."""

    instance_uuid: str
    instance_name: str
    from_host: str
    to_host: str
    weight: float
    improvement: float


@dataclass
class MigrationPlan:
    """Ordered list of proposed migrations for a single policy evaluation.

    The planner produces this; in M2 it is only logged (dry-run).
    In M3+ it will be published to oslo.messaging for the executor.
    """

    policy_name: str
    aggregate: str
    steps: list[MigrationStep] = field(default_factory=list)
    initial_imbalance: float = 0.0
    projected_imbalance: float = 0.0

    @property
    def migration_count(self) -> int:
        return len(self.steps)


@dataclass
class PolicyResult:
    """Result of evaluating a single policy."""

    policy_name: str
    mode: PolicyMode
    aggregate: str
    host_scores: list[HostScore]
    imbalance: float
    imbalance_detected: bool
    timestamp: datetime
    evaluation_duration_ms: float
    skipped: bool = False
    skip_reason: str = ""
    vm_profiles: dict[str, VmProfile] = field(default_factory=dict)
    migration_plan: MigrationPlan | None = None


@dataclass
class CycleReport:
    """Report from a single engine evaluation cycle."""

    cycle_number: int
    started_at: datetime
    completed_at: datetime
    policy_results: list[PolicyResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

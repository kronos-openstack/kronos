"""Core data types for the Kronos engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kronos.policies.models import PolicyMode


@dataclass
class HostScore:
    """Score for a single host from a policy evaluation."""

    host: str
    raw_score: float
    normalized_score: float


@dataclass
class VmProfile:
    """Resource profile for a single VM across all policies.

    Each policy contributes one entry to ``weights`` with its own view of
    this VM's resource footprint (memory RSS ratio, CPU rate ratio, etc.).
    The planner simulates moves by subtracting ``weights[policy_name]`` from
    the source host's score for that policy and adding it to the destination.
    ``sources`` tracks whether the value came from Prometheus or a fallback
    for debugging.
    """

    instance_uuid: str
    instance_name: str
    host: str
    weights: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class MigrationStep:
    """A single proposed VM migration within a plan."""

    instance_uuid: str
    instance_name: str
    from_host: str
    to_host: str
    improvement: float


@dataclass
class MigrationPlan:
    """Ordered list of proposed migrations for a single aggregate cycle.

    Covers all policies for the aggregate (combined scoring).  The engine
    emits one plan per aggregate per cycle when imbalance is detected.
    """

    aggregate: str
    policy_names: list[str] = field(default_factory=list)
    steps: list[MigrationStep] = field(default_factory=list)
    initial_imbalance: float = 0.0
    projected_imbalance: float = 0.0

    @property
    def migration_count(self) -> int:
        return len(self.steps)


@dataclass
class PolicyResult:
    """Result of evaluating a single policy against an aggregate."""

    policy_name: str
    mode: PolicyMode
    host_scores: list[HostScore]
    imbalance: float
    imbalance_detected: bool
    timestamp: datetime
    evaluation_duration_ms: float
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class AggregateResult:
    """Combined result of evaluating all policies against an aggregate."""

    aggregate: str
    policy_results: list[PolicyResult] = field(default_factory=list)
    combined_imbalance: float = 0.0
    imbalance_detected: bool = False
    vm_profiles: dict[str, VmProfile] = field(default_factory=dict)
    migration_plan: MigrationPlan | None = None


@dataclass
class CycleReport:
    """Report from a single engine evaluation cycle."""

    cycle_number: int
    started_at: datetime
    completed_at: datetime
    aggregate_results: list[AggregateResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True


@dataclass
class MigrationTask:
    """A single migration to execute, cast by the engine to the executor."""

    task_id: str
    plan_id: str
    aggregate: str
    policy_names: list[str]
    instance_uuid: str
    instance_name: str
    from_host: str
    to_host: str
    improvement: float
    priority: int = 5
    max_retries: int = 3
    retry_count: int = 0
    not_before: float = 0.0
    created_at: str = ""

    @staticmethod
    def from_step(
        step: MigrationStep,
        plan_id: str,
        aggregate: str,
        policy_names: list[str],
        not_before: float,
        max_retries: int = 3,
    ) -> MigrationTask:
        """Create a MigrationTask from a MigrationStep."""
        return MigrationTask(
            task_id=str(uuid.uuid4()),
            plan_id=plan_id,
            aggregate=aggregate,
            policy_names=list(policy_names),
            instance_uuid=step.instance_uuid,
            instance_name=step.instance_name,
            from_host=step.from_host,
            to_host=step.to_host,
            improvement=step.improvement,
            not_before=not_before,
            max_retries=max_retries,
            created_at=datetime.now(tz=UTC).isoformat(),
        )


@dataclass
class MigrationResult:
    """Outcome of a migration attempt, published by the executor.

    Engines (active and passive) listen for these to update cooldown state.
    """

    task_id: str
    plan_id: str
    aggregate: str
    instance_uuid: str
    from_host: str
    to_host: str
    success: bool
    error: str = ""
    duration_seconds: float = 0.0
    retry_count: int = 0
    completed_at: str = ""

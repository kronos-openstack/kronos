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


@dataclass
class CycleReport:
    """Report from a single engine evaluation cycle."""

    cycle_number: int
    started_at: datetime
    completed_at: datetime
    policy_results: list[PolicyResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

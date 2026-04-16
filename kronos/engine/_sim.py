"""Shared simulation helpers used by the imbalance planner and the
affinity enforcer.

Both components reason about per-policy host scores and candidate VM
moves.  The math is identical; only the move-generation strategy
differs.  These helpers are internal to the engine package.
"""

from __future__ import annotations

from kronos.engine.types import MigrationStep, VmProfile
from kronos.policies.models import PolicyConfig

# Per-policy score state: {policy_name: {host: score}}
PolicyScores = dict[str, dict[str, float]]


def combined_imbalance(
    scores: PolicyScores,
    policies: list[PolicyConfig],
) -> float:
    """Weighted sum of per-policy (max - min) imbalance values."""
    total = 0.0
    for policy in policies:
        host_scores = scores[policy.name]
        if len(host_scores) < 2:
            continue
        values = host_scores.values()
        total += policy.weight * (max(values) - min(values))
    return total


def policy_imbalance(scores: PolicyScores, policy_name: str) -> float:
    """Return the current (max - min) imbalance for one policy."""
    host_scores = scores[policy_name]
    if len(host_scores) < 2:
        return 0.0
    return max(host_scores.values()) - min(host_scores.values())


def all_policies_happy(
    scores: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True when every policy's imbalance is at or below its threshold."""
    return all(
        policy_imbalance(scores, policy.name) <= policy.threshold
        for policy in policies
    )


def move_hurts_any_policy(
    before: PolicyScores,
    after: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True if the move worsens any policy while leaving it above threshold.

    A move is allowed to slightly worsen a policy as long as it stays
    below that policy's threshold; and it is always allowed to improve
    or leave a policy unchanged.  Rejected only when both:

    * the policy's imbalance increases, AND
    * the new imbalance is above the policy's threshold.
    """
    for policy in policies:
        before_val = policy_imbalance(before, policy.name)
        after_val = policy_imbalance(after, policy.name)
        if after_val > before_val and after_val > policy.threshold:
            return True
    return False


def combined_host_score(
    scores: PolicyScores,
    policies: list[PolicyConfig],
    host: str,
) -> float:
    """Weighted sum of per-policy scores for a single host."""
    return sum(
        policy.weight * scores[policy.name].get(host, 0.0)
        for policy in policies
    )


def combined_vm_weight(
    vm: VmProfile,
    policies: list[PolicyConfig],
) -> float:
    """Weighted sum of per-policy VM weights."""
    return sum(
        policy.weight * vm.weights.get(policy.name, 0.0)
        for policy in policies
    )


def group_vms_by_host(
    vm_profiles: dict[str, VmProfile],
) -> dict[str, list[VmProfile]]:
    """Index VM profiles by current host."""
    by_host: dict[str, list[VmProfile]] = {}
    for vm in vm_profiles.values():
        by_host.setdefault(vm.host, []).append(vm)
    return by_host


def simulate_move(
    scores: PolicyScores,
    vm: VmProfile,
    from_host: str,
    to_host: str,
) -> PolicyScores:
    """Return a new per-policy score state reflecting a VM move."""
    new_scores: PolicyScores = {}
    for policy_name, host_scores in scores.items():
        weight = vm.weights.get(policy_name, 0.0)
        updated = dict(host_scores)
        updated[from_host] = updated.get(from_host, 0.0) - weight
        updated[to_host] = updated.get(to_host, 0.0) + weight
        new_scores[policy_name] = updated
    return new_scores


def apply_move_to_scores(
    scores: PolicyScores,
    new_scores: PolicyScores,
) -> None:
    """Replace the contents of ``scores`` in place with ``new_scores``."""
    for policy_name, host_scores in new_scores.items():
        scores[policy_name] = host_scores


def move_vm_between_hosts(
    vms_by_host: dict[str, list[VmProfile]],
    step: MigrationStep,
) -> None:
    """Update the VM-by-host index after a simulated move."""
    source_vms = vms_by_host.get(step.from_host, [])
    moved_vm: VmProfile | None = None
    for i, vm in enumerate(source_vms):
        if vm.instance_uuid == step.instance_uuid:
            moved_vm = source_vms.pop(i)
            break

    if moved_vm is not None:
        moved_vm.host = step.to_host
        vms_by_host.setdefault(step.to_host, []).append(moved_vm)

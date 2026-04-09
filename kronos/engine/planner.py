"""Migration planning with combined multi-policy scoring.

The planner receives all enabled policies for an aggregate plus a unified
set of VM profiles (each VM carries per-policy weights).  It simulates
candidate moves against every policy's scores simultaneously, picking
moves that reduce the weighted combined imbalance without pushing any
individual policy's imbalance above its own threshold.

Two strategies:

* **spread**: greedy best-move-per-round, minimising the combined imbalance
* **pack**: First Fit Decreasing, using combined utilization for host
  ordering and each policy's ``capacity_threshold`` as a ceiling
"""

from __future__ import annotations

from oslo_log import log as logging

from kronos.engine.constraints import ConstraintChecker
from kronos.engine.types import (
    MigrationPlan,
    MigrationStep,
    PolicyResult,
    VmProfile,
)
from kronos.policies.models import PolicyConfig, PolicyMode

LOG = logging.getLogger(__name__)


# Per-policy score state: {policy_name: {host: score}}
PolicyScores = dict[str, dict[str, float]]


class Planner:
    """Produces combined-scoring migration plans."""

    def __init__(self, constraints: ConstraintChecker) -> None:
        self._constraints = constraints

    def plan(
        self,
        aggregate: str,
        policies: list[PolicyConfig],
        policy_results: list[PolicyResult],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationPlan:
        """Generate a combined-scoring migration plan for an aggregate.

        :param aggregate: Aggregate name (for the plan metadata).
        :param policies: All enabled policies for this aggregate.
        :param policy_results: Scorer results for the same policies, in
            the same order.
        :param vm_profiles: Keyed by instance UUID; each profile has
            per-policy ``weights`` for every policy in ``policies``.
        :returns: MigrationPlan with proposed steps.
        """
        if not policies or not vm_profiles:
            return MigrationPlan(
                aggregate=aggregate,
                policy_names=[p.name for p in policies],
            )

        # Drop skipped policies from the set the planner reasons about.
        active: list[tuple[PolicyConfig, PolicyResult]] = [
            (p, r) for p, r in zip(policies, policy_results, strict=True)
            if not r.skipped and r.host_scores
        ]
        if not active:
            return MigrationPlan(
                aggregate=aggregate,
                policy_names=[p.name for p in policies],
            )

        active_policies = [p for p, _ in active]
        scores: PolicyScores = {
            p.name: {hs.host: hs.raw_score for hs in r.host_scores}
            for p, r in active
        }

        mode = active_policies[0].mode
        max_migrations = max(p.max_migrations_per_cycle for p in active_policies)

        plan = MigrationPlan(
            aggregate=aggregate,
            policy_names=[p.name for p in active_policies],
            initial_imbalance=_combined_imbalance(scores, active_policies),
        )

        if mode == PolicyMode.SPREAD:
            self._plan_spread(active_policies, scores, vm_profiles, max_migrations, plan)
        elif mode == PolicyMode.PACK:
            self._plan_pack(active_policies, scores, vm_profiles, max_migrations, plan)

        plan.projected_imbalance = _combined_imbalance(scores, active_policies)
        return plan

    # --- Spread ---

    def _plan_spread(
        self,
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vm_profiles: dict[str, VmProfile],
        max_migrations: int,
        plan: MigrationPlan,
    ) -> None:
        """Greedy spread planner over combined scoring.

        Each round picks the single best-improving (vm, source, dest)
        triple.  Stops when every policy's imbalance is below threshold,
        ``max_migrations`` is reached, or no improving move exists.
        """
        vms_by_host = _group_vms_by_host(vm_profiles)

        for _ in range(max_migrations):
            if _all_policies_happy(scores, policies):
                break

            best = self._find_best_spread_move(
                policies, scores, vms_by_host,
            )
            if best is None:
                break

            step, new_scores = best
            plan.steps.append(step)
            _apply_move_to_scores(scores, new_scores)
            _move_vm_between_hosts(vms_by_host, step)

    def _find_best_spread_move(
        self,
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vms_by_host: dict[str, list[VmProfile]],
    ) -> tuple[MigrationStep, PolicyScores] | None:
        """Find the single best move minimising combined imbalance.

        A candidate move is rejected if it would make any individual
        policy worse than it currently is while also being above that
        policy's own threshold.  This allows trade-offs between
        dimensions (e.g. a small CPU cost to fix a large memory
        imbalance) but never pushes a previously-OK policy into
        violation, or worsens an already-violating policy further.
        """
        current_combined = _combined_imbalance(scores, policies)
        best_step: MigrationStep | None = None
        best_scores: PolicyScores | None = None
        best_improvement = 0.0

        all_hosts = list(scores[policies[0].name].keys())

        for source_host in all_hosts:
            source_vms = vms_by_host.get(source_host, [])
            if not source_vms:
                continue

            for vm in source_vms:
                for dest_host in all_hosts:
                    if dest_host == source_host:
                        continue

                    if not self._constraints.check(vm, dest_host, vms_by_host):
                        continue

                    simulated = _simulate_move(scores, vm, source_host, dest_host)

                    if _move_hurts_any_policy(scores, simulated, policies):
                        continue

                    new_combined = _combined_imbalance(simulated, policies)
                    improvement = current_combined - new_combined

                    if improvement > best_improvement:
                        best_improvement = improvement
                        best_scores = simulated
                        best_step = MigrationStep(
                            instance_uuid=vm.instance_uuid,
                            instance_name=vm.instance_name,
                            from_host=source_host,
                            to_host=dest_host,
                            improvement=improvement,
                        )

        if best_step is None or best_scores is None:
            return None
        return best_step, best_scores

    # --- Pack ---

    def _plan_pack(
        self,
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vm_profiles: dict[str, VmProfile],
        max_migrations: int,
        plan: MigrationPlan,
    ) -> None:
        """First Fit Decreasing pack planner over combined scoring.

        Host ordering uses the combined weighted score.  A candidate
        destination must not breach any policy's ``capacity_threshold``.
        """
        vms_by_host = _group_vms_by_host(vm_profiles)

        # Snapshot of original residents — only original VMs are drained.
        original_vms_by_host: dict[str, list[VmProfile]] = {
            host: list(vms) for host, vms in vms_by_host.items()
        }

        draining: set[str] = set()

        # Coldest-first by combined score
        all_hosts = list(scores[policies[0].name].keys())
        sorted_hosts = sorted(
            all_hosts,
            key=lambda h: _combined_host_score(scores, policies, h),
        )

        migrations_done = 0
        for source_host in sorted_hosts:
            if migrations_done >= max_migrations:
                break

            source_vms = list(original_vms_by_host.get(source_host, []))
            if not source_vms:
                continue

            # Biggest combined-weight VMs first
            source_vms.sort(
                key=lambda v: _combined_vm_weight(v, policies),
                reverse=True,
            )
            draining.add(source_host)

            for vm in source_vms:
                if migrations_done >= max_migrations:
                    break

                dest = self._find_pack_destination(
                    policies, scores, vm, draining, vms_by_host,
                )
                if dest is None:
                    continue

                step = MigrationStep(
                    instance_uuid=vm.instance_uuid,
                    instance_name=vm.instance_name,
                    from_host=source_host,
                    to_host=dest,
                    improvement=0.0,
                )
                plan.steps.append(step)

                new_scores = _simulate_move(scores, vm, source_host, dest)
                _apply_move_to_scores(scores, new_scores)
                _move_vm_between_hosts(vms_by_host, step)
                migrations_done += 1

    def _find_pack_destination(
        self,
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vm: VmProfile,
        draining: set[str],
        vms_by_host: dict[str, list[VmProfile]],
    ) -> str | None:
        """Fullest non-draining host that fits within every policy's capacity."""
        all_hosts = list(scores[policies[0].name].keys())
        candidates = sorted(
            (h for h in all_hosts if h not in draining and h != vm.host),
            key=lambda h: _combined_host_score(scores, policies, h),
            reverse=True,
        )

        for host in candidates:
            fits = True
            for policy in policies:
                projected = scores[policy.name][host] + vm.weights.get(policy.name, 0.0)
                if projected > policy.capacity_threshold:
                    fits = False
                    break
            if not fits:
                continue

            if not self._constraints.check(vm, host, vms_by_host):
                continue
            return host

        return None


# --- Helpers ---


def _combined_imbalance(
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


def _all_policies_happy(
    scores: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True when every policy's imbalance is at or below its threshold."""
    for policy in policies:
        host_scores = scores[policy.name]
        if len(host_scores) < 2:
            continue
        imbalance = max(host_scores.values()) - min(host_scores.values())
        if imbalance > policy.threshold:
            return False
    return True


def _policy_imbalance(
    scores: PolicyScores,
    policy_name: str,
) -> float:
    """Return the current (max - min) imbalance for one policy."""
    host_scores = scores[policy_name]
    if len(host_scores) < 2:
        return 0.0
    return max(host_scores.values()) - min(host_scores.values())


def _move_hurts_any_policy(
    before: PolicyScores,
    after: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True if the move worsens any policy while leaving it above threshold.

    A move is allowed to slightly worsen a policy as long as it stays
    below that policy's threshold; and it is always allowed to improve
    or leave a policy unchanged.  It is only rejected when both:

    * the policy's imbalance increases, AND
    * the new imbalance is above the policy's threshold
    """
    for policy in policies:
        before_val = _policy_imbalance(before, policy.name)
        after_val = _policy_imbalance(after, policy.name)
        if after_val > before_val and after_val > policy.threshold:
            return True
    return False


def _combined_host_score(
    scores: PolicyScores,
    policies: list[PolicyConfig],
    host: str,
) -> float:
    """Weighted sum of per-policy scores for a single host."""
    return sum(
        policy.weight * scores[policy.name].get(host, 0.0)
        for policy in policies
    )


def _combined_vm_weight(
    vm: VmProfile,
    policies: list[PolicyConfig],
) -> float:
    """Weighted sum of per-policy VM weights."""
    return sum(
        policy.weight * vm.weights.get(policy.name, 0.0)
        for policy in policies
    )


def _group_vms_by_host(
    vm_profiles: dict[str, VmProfile],
) -> dict[str, list[VmProfile]]:
    by_host: dict[str, list[VmProfile]] = {}
    for vm in vm_profiles.values():
        by_host.setdefault(vm.host, []).append(vm)
    return by_host


def _simulate_move(
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


def _apply_move_to_scores(
    scores: PolicyScores,
    new_scores: PolicyScores,
) -> None:
    """Replace the contents of ``scores`` in place with ``new_scores``."""
    for policy_name, host_scores in new_scores.items():
        scores[policy_name] = host_scores


def _move_vm_between_hosts(
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

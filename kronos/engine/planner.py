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

from kronos.engine._sim import (
    PolicyScores,
    all_policies_happy,
    apply_move_to_scores,
    combined_host_score,
    combined_imbalance,
    combined_vm_weight,
    group_vms_by_host,
    move_hurts_any_policy,
    move_vm_between_hosts,
    simulate_move,
)
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.types import (
    MigrationPhase,
    MigrationPlan,
    MigrationStep,
    PolicyResult,
    VmProfile,
)
from kronos.policies.models import PolicyConfig, PolicyMode

LOG = logging.getLogger(__name__)


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
        scores: PolicyScores | None = None,
        vms_by_host: dict[str, list[VmProfile]] | None = None,
        budget: int | None = None,
        plan: MigrationPlan | None = None,
    ) -> MigrationPlan:
        """Generate a combined-scoring migration plan for an aggregate.

        :param aggregate: Aggregate name (for the plan metadata).
        :param policies: All enabled policies for this aggregate.
        :param policy_results: Scorer results for the same policies, in
            the same order.
        :param vm_profiles: Keyed by instance UUID; each profile has
            per-policy ``weights`` for every policy in ``policies``.
        :param scores: Optional pre-simulated per-policy scores (e.g.
            the state after the affinity enforcer applied its moves).
            When provided, the planner starts from this state instead
            of rebuilding it from ``policy_results``.
        :param vms_by_host: Optional pre-simulated VM placement index.
            Must be consistent with ``scores`` when both are given.
        :param budget: Optional cap on migrations to emit.  Defaults to
            the max of ``max_migrations_per_cycle`` across active
            policies.  When smaller than that, caps the planner's
            greedy/pack loop accordingly.
        :param plan: Optional existing plan to append to (enforcer
            prepends its steps; this planner extends after).  When
            omitted, a fresh plan is created.
        :returns: MigrationPlan with proposed steps (the same object as
            ``plan`` when it was provided).
        """
        fresh_plan = plan is None
        if plan is None:
            plan = MigrationPlan(
                aggregate=aggregate,
                policy_names=[p.name for p in policies],
            )

        if not policies or not vm_profiles:
            return plan

        # Drop skipped policies from the set the planner reasons about.
        active: list[tuple[PolicyConfig, PolicyResult]] = [
            (p, r) for p, r in zip(policies, policy_results, strict=True)
            if not r.skipped and r.host_scores
        ]
        if not active:
            return plan

        active_policies = [p for p, _ in active]
        if scores is None:
            scores = {
                p.name: {hs.host: hs.raw_score for hs in r.host_scores}
                for p, r in active
            }

        mode = active_policies[0].mode
        default_budget = max(p.max_migrations_per_cycle for p in active_policies)
        if budget is None:
            budget = default_budget
        if budget <= 0:
            LOG.debug(
                "Imbalance planner budget exhausted for aggregate %s",
                aggregate,
            )
            return plan

        if fresh_plan:
            plan.initial_imbalance = combined_imbalance(scores, active_policies)

        if vms_by_host is None:
            vms_by_host = group_vms_by_host(vm_profiles)

        if mode == PolicyMode.SPREAD:
            self._plan_spread(
                active_policies, scores, vms_by_host, budget, plan,
            )
        elif mode == PolicyMode.PACK:
            self._plan_pack(
                active_policies, scores, vm_profiles, vms_by_host,
                budget, plan,
            )

        plan.projected_imbalance = combined_imbalance(scores, active_policies)
        return plan

    # --- Spread ---

    def _plan_spread(
        self,
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vms_by_host: dict[str, list[VmProfile]],
        budget: int,
        plan: MigrationPlan,
    ) -> None:
        """Greedy spread planner over combined scoring.

        Each round picks the single best-improving (vm, source, dest)
        triple.  Stops when every policy's imbalance is below threshold,
        ``budget`` is reached, or no improving move exists.
        """
        for _ in range(budget):
            if all_policies_happy(scores, policies):
                break

            best = self._find_best_spread_move(
                policies, scores, vms_by_host,
            )
            if best is None:
                break

            step, new_scores = best
            plan.steps.append(step)
            apply_move_to_scores(scores, new_scores)
            move_vm_between_hosts(vms_by_host, step)

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
        current_combined = combined_imbalance(scores, policies)
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

                    simulated = simulate_move(scores, vm, source_host, dest_host)

                    if move_hurts_any_policy(scores, simulated, policies):
                        continue

                    new_combined = combined_imbalance(simulated, policies)
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
                            phase=MigrationPhase.SPREAD,
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
        vms_by_host: dict[str, list[VmProfile]],
        budget: int,
        plan: MigrationPlan,
    ) -> None:
        """First Fit Decreasing pack planner over combined scoring.

        Host ordering uses the combined weighted score.  A candidate
        destination must not breach any policy's ``capacity_threshold``.
        """
        # Snapshot of original residents — only original VMs are drained.
        original_vms_by_host: dict[str, list[VmProfile]] = {
            host: list(vms) for host, vms in vms_by_host.items()
        }

        draining: set[str] = set()

        # Coldest-first by combined score
        all_hosts = list(scores[policies[0].name].keys())
        sorted_hosts = sorted(
            all_hosts,
            key=lambda h: combined_host_score(scores, policies, h),
        )

        migrations_done = 0
        for source_host in sorted_hosts:
            if migrations_done >= budget:
                break

            source_vms = list(original_vms_by_host.get(source_host, []))
            if not source_vms:
                continue

            # Biggest combined-weight VMs first
            source_vms.sort(
                key=lambda v: combined_vm_weight(v, policies),
                reverse=True,
            )
            draining.add(source_host)

            for vm in source_vms:
                if migrations_done >= budget:
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
                    phase=MigrationPhase.PACK,
                )
                plan.steps.append(step)

                new_scores = simulate_move(scores, vm, source_host, dest)
                apply_move_to_scores(scores, new_scores)
                move_vm_between_hosts(vms_by_host, step)
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
            key=lambda h: combined_host_score(scores, policies, h),
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

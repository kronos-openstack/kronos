"""Affinity enforcement: proactively repair Nova server-group violations.

Where the :class:`~kronos.engine.planner.Planner` moves VMs to reduce
PromQL-driven imbalance, the :class:`AffinityEnforcer` moves VMs to
bring the current placement back into compliance with Nova server
group rules.

The enforcer is rule-driven: it decides *which* VMs must move.  The
destination for each mandatory move is chosen by the same combined
imbalance math the planner uses — so a repair never worsens a policy
past its threshold.  Destinations that would break another server
group policy for the same VM are rejected by the shared
:class:`~kronos.engine.constraints.ConstraintChecker`.

Enabled via ``[engine] enforce_hard_affinity`` and
``[engine] enforce_soft_affinity`` in ``kronos.conf``.  Both default
to false.

The enforcer runs before the imbalance planner and they share
``max_migrations_per_cycle`` as a single budget: repair steps consume
budget first, the planner gets the remainder.
"""

from __future__ import annotations

from dataclasses import dataclass

from oslo_log import log as logging

from kronos.engine._sim import (
    PolicyScores,
    combined_imbalance,
    group_vms_by_host,
    move_hurts_any_policy,
    move_vm_between_hosts,
    simulate_move,
)
from kronos.engine.constraints import (
    AFFINITY_POLICIES,
    ANTI_AFFINITY_POLICIES,
    ConstraintChecker,
    ServerGroup,
)
from kronos.engine.types import (
    MigrationPhase,
    MigrationPlan,
    MigrationStep,
    PolicyResult,
    VmProfile,
)
from kronos.policies.models import PolicyConfig

LOG = logging.getLogger(__name__)


HARD_POLICIES = frozenset({"affinity", "anti-affinity"})
SOFT_POLICIES = frozenset({"soft-affinity", "soft-anti-affinity"})


@dataclass(frozen=True)
class _Violation:
    """A single server-group rule in the wrong state right now."""

    group: ServerGroup
    offending_uuids: frozenset[str]


class AffinityEnforcer:
    """Emit migrations that restore server-group compliance.

    :param constraints: Shared constraint checker (provides parsed
        server groups and move-legality checks).
    :param enforce_hard: Honour ``affinity`` and ``anti-affinity``.
    :param enforce_soft: Honour ``soft-affinity`` and
        ``soft-anti-affinity``.
    """

    def __init__(
        self,
        constraints: ConstraintChecker,
        *,
        enforce_hard: bool,
        enforce_soft: bool,
    ) -> None:
        self._constraints = constraints
        self._enforce_hard = enforce_hard
        self._enforce_soft = enforce_soft

    @property
    def enabled(self) -> bool:
        return self._enforce_hard or self._enforce_soft

    def enforce(
        self,
        aggregate: str,
        policies: list[PolicyConfig],
        policy_results: list[PolicyResult],
        vm_profiles: dict[str, VmProfile],
        budget: int,
    ) -> tuple[MigrationPlan, PolicyScores, dict[str, list[VmProfile]], int]:
        """Plan repair migrations for this aggregate.

        :returns: ``(plan, scores, vms_by_host, remaining_budget)``.
            ``scores`` and ``vms_by_host`` reflect the simulated state
            after all repair steps have been applied — the imbalance
            planner should start from them.  ``remaining_budget`` is
            what's left of ``budget`` after repairs.
        """
        plan = MigrationPlan(
            aggregate=aggregate,
            policy_names=[p.name for p in policies],
        )

        active: list[tuple[PolicyConfig, PolicyResult]] = [
            (p, r) for p, r in zip(policies, policy_results, strict=True)
            if not r.skipped and r.host_scores
        ]
        active_policies = [p for p, _ in active]
        scores: PolicyScores = {
            p.name: {hs.host: hs.raw_score for hs in r.host_scores}
            for p, r in active
        }
        vms_by_host = group_vms_by_host(vm_profiles)

        if not self.enabled or budget <= 0 or not active_policies:
            return plan, scores, vms_by_host, budget

        plan.initial_imbalance = combined_imbalance(scores, active_policies)

        relevant = self._select_groups()
        if not relevant:
            return plan, scores, vms_by_host, budget

        steps_used = 0
        # Repeat: violations can cascade (fixing one may un-violate a
        # sibling, or expose a new one).  Re-detect from scratch every
        # iteration so the view is always truthful.
        while steps_used < budget:
            violations = _detect_violations(relevant, vms_by_host)
            if not violations:
                break

            step = self._plan_one_repair(
                violations,
                active_policies,
                scores,
                vms_by_host,
                vm_profiles,
            )
            if step is None:
                # No legal destination for any current violation — log
                # and stop; imbalance planner still gets the remaining
                # budget.
                LOG.warning(
                    "[AFFINITY] aggregate=%s %d violation(s) could not be "
                    "repaired without crossing policy thresholds",
                    aggregate,
                    len(violations),
                )
                break

            LOG.info(
                "[AFFINITY] aggregate=%s move %s (%s) %s -> %s "
                "(combined %.3f -> %.3f)",
                aggregate,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                combined_imbalance(scores, active_policies) + step.improvement,
                combined_imbalance(scores, active_policies),
            )
            plan.steps.append(step)
            steps_used += 1

        plan.projected_imbalance = combined_imbalance(scores, active_policies)
        return plan, scores, vms_by_host, budget - steps_used

    # --- Internals ---

    def _select_groups(self) -> list[ServerGroup]:
        """Return groups whose policy is enabled for enforcement."""
        keep: set[str] = set()
        if self._enforce_hard:
            keep |= HARD_POLICIES
        if self._enforce_soft:
            keep |= SOFT_POLICIES
        return [g for g in self._constraints.get_groups() if g.policy in keep]

    def _plan_one_repair(
        self,
        violations: list[_Violation],
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vms_by_host: dict[str, list[VmProfile]],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationStep | None:
        """Simulate every legal (offending_vm, dest) pair and pick the
        move that leaves the combined imbalance lowest.

        Destinations that would push any policy past its threshold are
        rejected (``move_hurts_any_policy``), as are destinations that
        violate any *other* server group the VM belongs to.
        """
        # Candidate VMs = the union of offending UUIDs across all
        # violations.  A single VM may be offending multiple groups at
        # once; moving it fixes all of them in one step.
        candidate_uuids: set[str] = set()
        for v in violations:
            candidate_uuids |= v.offending_uuids

        hosts = list(scores[policies[0].name].keys())
        current_combined = combined_imbalance(scores, policies)

        best_step: MigrationStep | None = None
        best_combined = float("inf")

        # Deterministic iteration for stable tie-breaks.
        for uuid in sorted(candidate_uuids):
            vm = vm_profiles.get(uuid)
            if vm is None:
                continue
            for dest in sorted(hosts):
                if dest == vm.host:
                    continue

                # Legality: every group the VM is a member of must
                # still be satisfied at the destination.
                if not self._constraints.check(vm, dest, vms_by_host):
                    continue

                simulated = simulate_move(scores, vm, vm.host, dest)
                if move_hurts_any_policy(scores, simulated, policies):
                    continue

                new_combined = combined_imbalance(simulated, policies)
                # Strict improvement tie-break with sorted iteration
                # gives determinism.
                if new_combined < best_combined:
                    best_combined = new_combined
                    best_step = MigrationStep(
                        instance_uuid=vm.instance_uuid,
                        instance_name=vm.instance_name,
                        from_host=vm.host,
                        to_host=dest,
                        improvement=current_combined - new_combined,
                        phase=MigrationPhase.AFFINITY,
                    )

        if best_step is None:
            return None

        # Apply to the simulation state so the next iteration and the
        # downstream imbalance planner see the post-move world.
        applied = simulate_move(
            scores,
            vm_profiles[best_step.instance_uuid],
            best_step.from_host,
            best_step.to_host,
        )
        for policy_name, host_scores in applied.items():
            scores[policy_name] = host_scores
        move_vm_between_hosts(vms_by_host, best_step)
        return best_step


def _detect_violations(
    groups: list[ServerGroup],
    vms_by_host: dict[str, list[VmProfile]],
) -> list[_Violation]:
    """Return groups currently violating their policy.

    Only members visible in ``vms_by_host`` count; members outside the
    aggregate are ignored (consistent with the constraint checker).
    """
    # Index visible members: uuid -> host
    placement: dict[str, str] = {}
    for host, vms in vms_by_host.items():
        for vm in vms:
            placement[vm.instance_uuid] = host

    out: list[_Violation] = []
    for group in groups:
        visible = group.members & placement.keys()
        if len(visible) < 2:
            # A single visible member can't violate affinity or anti-
            # affinity against siblings.
            continue

        hosts_used = {placement[u] for u in visible}

        if group.policy in ANTI_AFFINITY_POLICIES:
            # Violation when two or more members share a host.
            offenders = _anti_affinity_offenders(visible, placement)
            if offenders:
                out.append(
                    _Violation(group=group, offending_uuids=frozenset(offenders)),
                )
        elif group.policy in AFFINITY_POLICIES:
            # Violation when members span more than one host.
            if len(hosts_used) > 1:
                out.append(
                    _Violation(group=group, offending_uuids=frozenset(visible)),
                )
    return out


def _anti_affinity_offenders(
    member_uuids: frozenset[str] | set[str],
    placement: dict[str, str],
) -> set[str]:
    """Every member that shares a host with at least one sibling.

    All co-located members are flagged as offenders; which one to
    actually move is decided by the destination-scoring pass.
    """
    by_host: dict[str, list[str]] = {}
    for uuid in member_uuids:
        by_host.setdefault(placement[uuid], []).append(uuid)
    offenders: set[str] = set()
    for uuids in by_host.values():
        if len(uuids) > 1:
            offenders.update(uuids)
    return offenders

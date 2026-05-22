"""Evacuation of VMs off administratively-disabled compute hosts.

When a Nova operator disables a compute service
(``openstack compute service set --disable ...``) they want existing
VMs *off* that host - typically ahead of maintenance.  Nova alone does
not migrate them; new placements stop landing there but existing VMs
stay put until something moves them.

The evacuator fills that gap.  When ``[engine] evacuate_disabled_hosts``
is set, every cycle we look at the per-host nova-compute service state,
identify VMs that live on a ``status=disabled`` host *inside the
aggregate under evaluation*, and propose moves to legal destinations.
The math is the same as the affinity enforcer: pick the destination
that minimises combined imbalance without pushing any policy past its
threshold and without breaking a server-group constraint.

The evacuator runs *before* the affinity enforcer and the imbalance
planner.  All three share the per-cycle ``max_migrations_per_cycle``
budget; the evacuator consumes first because draining a disabled host
is the strongest operator signal.

Hosts whose service is ``state=down`` are not evacuation candidates -
live migration cannot move VMs off a hypervisor that isn't running.
The constraint checker still excludes such hosts as destinations.

Aggregate scoping: the per-cycle service map is cluster-wide, so the
evacuator intersects it with the aggregate's own host list before
selecting candidates.  Without that join we would scan disabled hosts
from other aggregates / AZs and silently include their VMs once they
appeared in ``vms_by_host`` for any reason.
"""

from __future__ import annotations

from collections.abc import Iterable

from oslo_log import log as logging

from kronos.clients.nova import ComputeService
from kronos.engine._sim import (
    PolicyScores,
    ScoreState,
    move_vm_between_hosts,
)
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.types import (
    MigrationPhase,
    MigrationPlan,
    MigrationStep,
    VmProfile,
)
from kronos.policies.models import PolicyConfig

LOG = logging.getLogger(__name__)


class Evacuator:
    """Plan migrations off administratively-disabled compute hosts.

    :param constraints: Shared constraint checker.  Provides destination
        availability (service is up + enabled) and server-group
        legality.
    :param enabled: When ``False`` the evacuator is a no-op.
    """

    def __init__(
        self,
        constraints: ConstraintChecker,
        *,
        enabled: bool,
    ) -> None:
        self._constraints = constraints
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def evacuate(
        self,
        aggregate: str,
        aggregate_hosts: Iterable[str],
        policies: list[PolicyConfig],
        scores: PolicyScores,
        vms_by_host: dict[str, list[VmProfile]],
        vm_profiles: dict[str, VmProfile],
        services: dict[str, ComputeService],
        budget: int,
    ) -> tuple[MigrationPlan, PolicyScores, dict[str, list[VmProfile]], int]:
        """Plan evacuation moves for VMs on disabled hosts in this aggregate.

        :param aggregate: Aggregate name (for log lines and plan metadata).
        :param aggregate_hosts: The host list for *this* aggregate.  The
            cluster-wide ``services`` map is intersected with this set so
            disabled hosts in unrelated aggregates / AZs are ignored.
        :param policies: Active enabled policies for the aggregate.
        :param scores: Per-policy host scores; mutated in place via
            ``state.apply`` for every accepted move.
        :param vms_by_host: Current placement index; mutated in place.
        :param vm_profiles: ``instance_uuid -> VmProfile`` lookup.
        :param services: Cluster-wide ``host -> ComputeService`` map
            (typically the same one installed on the constraint checker).
        :param budget: Maximum number of evacuation moves.
        :returns: ``(plan, scores, vms_by_host, remaining_budget)``.
            ``scores`` and ``vms_by_host`` reflect the simulated post-
            evacuation state; downstream stages start from them.
        """
        plan = MigrationPlan(
            aggregate=aggregate,
            policy_names=[p.name for p in policies],
        )

        if not self._enabled or budget <= 0 or not policies:
            return plan, scores, vms_by_host, budget

        scope = set(aggregate_hosts)
        # Disabled-and-still-reachable hosts in this aggregate.
        # state=down hosts are skipped - live migration cannot drain them.
        disabled_hosts = {
            host for host in scope
            if (svc := services.get(host)) is not None
            and svc.is_up and not svc.is_enabled
        }
        if not disabled_hosts:
            return plan, scores, vms_by_host, budget

        candidates: list[str] = sorted(
            vm.instance_uuid
            for host in disabled_hosts
            for vm in vms_by_host.get(host, [])
        )
        if not candidates:
            LOG.info(
                "Evacuation in '%s': %d disabled host(s) in scope "
                "but no VMs on them",
                aggregate,
                len(disabled_hosts),
            )
            return plan, scores, vms_by_host, budget

        LOG.info(
            "Evacuation in '%s': %d candidate VM(s) on %d disabled host(s)",
            aggregate,
            len(candidates),
            len(disabled_hosts),
        )

        state = ScoreState.from_scores(scores)
        plan.initial_imbalance = state.combined_imbalance(policies)

        steps_used = 0
        remaining = list(candidates)
        while steps_used < budget and remaining:
            step = self._plan_one_evacuation(
                remaining,
                policies,
                state,
                vms_by_host,
                vm_profiles,
            )
            if step is None:
                LOG.warning(
                    "Evacuation in '%s': %d VM(s) on disabled hosts "
                    "have no legal destination this cycle",
                    aggregate,
                    len(remaining),
                )
                break

            LOG.info(
                "Evacuation in '%s': %s (%s) %s -> %s "
                "(disabled host), combined imbalance %.3f -> %.3f",
                aggregate,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                state.combined_imbalance(policies) + step.improvement,
                state.combined_imbalance(policies),
            )
            plan.steps.append(step)
            steps_used += 1
            remaining = [u for u in remaining if u != step.instance_uuid]

        plan.projected_imbalance = state.combined_imbalance(policies)
        return plan, scores, vms_by_host, budget - steps_used

    def _plan_one_evacuation(
        self,
        candidate_uuids: list[str],
        policies: list[PolicyConfig],
        state: ScoreState,
        vms_by_host: dict[str, list[VmProfile]],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationStep | None:
        """Pick the (vm, dest) pair leaving combined imbalance lowest.

        Filters applied to every candidate pair:

        1. ``ConstraintChecker.check`` - destination is up + enabled
           (per the cycle's service map) and no server-group rule for
           the VM is broken.
        2. ``simulated_move_hurts_any_policy`` - the move must not
           push any policy's imbalance above its threshold while also
           worsening it.

        Among survivors, pick the lowest-combined-imbalance pair.
        Sorted iteration gives deterministic tie-breaks.
        """
        hosts = list(state.scores[policies[0].name].keys())
        current_combined = state.combined_imbalance(policies)

        best_step: MigrationStep | None = None
        best_combined = float("inf")

        for uuid in candidate_uuids:
            vm = vm_profiles.get(uuid)
            if vm is None:
                continue
            for dest in sorted(hosts):
                if dest == vm.host:
                    continue

                if not self._constraints.check(vm, dest, vms_by_host):
                    continue

                if state.simulated_move_hurts_any_policy(
                    vm, vm.host, dest, policies,
                ):
                    continue

                new_combined = state.simulated_combined_imbalance(
                    vm, vm.host, dest, policies,
                )
                if new_combined < best_combined:
                    best_combined = new_combined
                    best_step = MigrationStep(
                        instance_uuid=vm.instance_uuid,
                        instance_name=vm.instance_name,
                        from_host=vm.host,
                        to_host=dest,
                        improvement=current_combined - new_combined,
                        phase=MigrationPhase.EVACUATE,
                    )

        if best_step is None:
            return None

        moved_vm = vm_profiles[best_step.instance_uuid]
        state.apply(moved_vm, best_step.from_host, best_step.to_host)
        self._constraints.commit_move(
            moved_vm, best_step.from_host, best_step.to_host,
        )
        move_vm_between_hosts(vms_by_host, best_step)
        return best_step

"""Migration planning: simulation-based VM placement decisions.

The planner takes scored hosts and VM profiles, then simulates moves
to find an optimal set of migrations that reduce imbalance (spread)
or consolidate workloads (pack).

This module is a pure planner — it never calls Nova migrate.  In M2
it produces ``MigrationPlan`` objects that are logged (dry-run).  In
M3+ plans will be published to oslo.messaging for the executor.
"""

from __future__ import annotations

from oslo_log import log as logging

from kronos.engine.constraints import ConstraintChecker
from kronos.engine.types import (
    HostScore,
    MigrationPlan,
    MigrationStep,
    VmProfile,
)
from kronos.policies.models import PolicyConfig, PolicyMode

LOG = logging.getLogger(__name__)


class Planner:
    """Produces migration plans by simulating VM moves.

    Delegates to mode-specific strategies: greedy simulation for
    spread, First Fit Decreasing for pack.
    """

    def __init__(self, constraints: ConstraintChecker) -> None:
        self._constraints = constraints

    def plan(
        self,
        policy: PolicyConfig,
        host_scores: list[HostScore],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationPlan:
        """Generate a migration plan for the given policy.

        :param policy: The policy being evaluated.
        :param host_scores: Scored hosts (sorted descending by raw_score).
        :param vm_profiles: Per-VM profiles keyed by instance UUID.
        :returns: MigrationPlan with proposed steps.
        """
        if len(host_scores) < 2 or not vm_profiles:
            return MigrationPlan(
                policy_name=policy.name,
                aggregate=policy.aggregate,
            )

        if policy.mode == PolicyMode.SPREAD:
            return self._plan_spread(policy, host_scores, vm_profiles)
        if policy.mode == PolicyMode.PACK:
            return self._plan_pack(policy, host_scores, vm_profiles)

        return MigrationPlan(
            policy_name=policy.name,
            aggregate=policy.aggregate,
        )

    def _plan_spread(
        self,
        policy: PolicyConfig,
        host_scores: list[HostScore],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationPlan:
        """Greedy spread planner.

        On each iteration:
          1. Pick the hottest host as source.
          2. Pick the coldest host as destination.
          3. Try each VM on the source, simulate the move.
          4. Accept the move that yields the best imbalance reduction.
          5. Repeat up to ``max_migrations_per_cycle``.
        """
        # Build mutable score state: host -> current simulated score
        scores = {hs.host: hs.raw_score for hs in host_scores}
        # Group VM profiles by host
        vms_by_host = _group_vms_by_host(vm_profiles)

        plan = MigrationPlan(
            policy_name=policy.name,
            aggregate=policy.aggregate,
            initial_imbalance=_imbalance(scores),
        )

        for _ in range(policy.max_migrations_per_cycle):
            current_imbalance = _imbalance(scores)
            if current_imbalance <= policy.threshold:
                break

            best = self._find_best_spread_move(
                scores, vms_by_host, policy, current_imbalance,
            )
            if best is None:
                break

            step, new_scores = best
            plan.steps.append(step)

            # Apply the move to simulation state
            scores = new_scores
            _move_vm_between_hosts(vms_by_host, step)

        plan.projected_imbalance = _imbalance(scores)
        return plan

    def _find_best_spread_move(
        self,
        scores: dict[str, float],
        vms_by_host: dict[str, list[VmProfile]],
        policy: PolicyConfig,
        current_imbalance: float,
    ) -> tuple[MigrationStep, dict[str, float]] | None:
        """Find the single best VM move that reduces imbalance the most."""
        best_step: MigrationStep | None = None
        best_scores: dict[str, float] | None = None
        best_improvement = 0.0

        # Source: hottest host (highest score)
        sorted_hosts = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        for source_host, _ in sorted_hosts:
            source_vms = vms_by_host.get(source_host, [])
            if not source_vms:
                continue

            for vm in source_vms:
                # Try each host as destination (coldest first)
                for dest_host, _ in reversed(sorted_hosts):
                    if dest_host == source_host:
                        continue

                    if not self._constraints.check(vm, dest_host, vms_by_host):
                        continue

                    simulated = _simulate_move(scores, vm, source_host, dest_host)
                    new_imbalance = _imbalance(simulated)
                    improvement = current_imbalance - new_imbalance

                    if improvement > best_improvement:
                        best_improvement = improvement
                        best_scores = simulated
                        best_step = MigrationStep(
                            instance_uuid=vm.instance_uuid,
                            instance_name=vm.instance_name,
                            from_host=source_host,
                            to_host=dest_host,
                            weight=vm.weight,
                            improvement=improvement,
                        )

        if best_step is None or best_scores is None:
            return None
        return best_step, best_scores

    def _plan_pack(
        self,
        policy: PolicyConfig,
        host_scores: list[HostScore],
        vm_profiles: dict[str, VmProfile],
    ) -> MigrationPlan:
        """First Fit Decreasing bin-pack planner.

        Goal: consolidate VMs into the fewest hosts possible.
        No drain threshold — the emptiest hosts are drained first.

        Algorithm:
          1. Sort hosts ascending by utilisation (emptiest first = drain order).
          2. For each drain candidate, collect its VMs sorted biggest-first.
          3. For each VM, find the fullest non-source host where
             score + weight <= ``capacity_threshold``.
          4. Stop when ``max_migrations_per_cycle`` is reached or no
             more moves are possible.

        Hosts being drained are tracked so VMs are never moved *to* them.
        """
        scores = {hs.host: hs.raw_score for hs in host_scores}
        vms_by_host = _group_vms_by_host(vm_profiles)

        plan = MigrationPlan(
            policy_name=policy.name,
            aggregate=policy.aggregate,
            initial_imbalance=_imbalance(scores),
        )

        # Hosts we are draining — never send VMs to these
        draining: set[str] = set()

        # Snapshot which VMs each host originally owns.  VMs that
        # land on a host via a migration in this cycle must NOT be
        # drained again — only the original residents are candidates.
        original_vms_by_host: dict[str, list[VmProfile]] = {
            host: list(vms) for host, vms in vms_by_host.items()
        }

        # Process hosts coldest-first (by initial score)
        sorted_hosts = sorted(scores.items(), key=lambda x: x[1])

        migrations_done = 0
        for source_host, _ in sorted_hosts:
            if migrations_done >= policy.max_migrations_per_cycle:
                break

            source_vms = list(original_vms_by_host.get(source_host, []))
            if not source_vms:
                continue

            # Try to empty this host — biggest VMs first
            source_vms.sort(key=lambda v: v.weight, reverse=True)
            draining.add(source_host)

            for vm in source_vms:
                if migrations_done >= policy.max_migrations_per_cycle:
                    break

                dest = self._find_pack_destination(
                    scores, vm, draining, vms_by_host, policy,
                )
                if dest is None:
                    continue

                step = MigrationStep(
                    instance_uuid=vm.instance_uuid,
                    instance_name=vm.instance_name,
                    from_host=source_host,
                    to_host=dest,
                    weight=vm.weight,
                    improvement=0.0,
                )
                plan.steps.append(step)

                scores = _simulate_move(scores, vm, source_host, dest)
                _move_vm_between_hosts(vms_by_host, step)
                migrations_done += 1

        plan.projected_imbalance = _imbalance(scores)
        return plan

    def _find_pack_destination(
        self,
        scores: dict[str, float],
        vm: VmProfile,
        draining: set[str],
        vms_by_host: dict[str, list[VmProfile]],
        policy: PolicyConfig,
    ) -> str | None:
        """Find the fullest non-draining host that can fit the VM.

        Candidates are sorted fullest-first.  A host qualifies if
        ``score + vm.weight <= capacity_threshold`` and it passes
        constraint checks.
        """
        candidates = sorted(
            ((h, s) for h, s in scores.items() if h not in draining and h != vm.host),
            key=lambda x: x[1],
            reverse=True,
        )

        for host, score in candidates:
            if score + vm.weight > policy.capacity_threshold:
                continue
            if not self._constraints.check(vm, host, vms_by_host):
                continue
            return host

        return None


# --- Simulation helpers (module-level, stateless) ---


def _group_vms_by_host(
    vm_profiles: dict[str, VmProfile],
) -> dict[str, list[VmProfile]]:
    """Group VM profiles by their current host."""
    by_host: dict[str, list[VmProfile]] = {}
    for vm in vm_profiles.values():
        by_host.setdefault(vm.host, []).append(vm)
    return by_host


def _simulate_move(
    scores: dict[str, float],
    vm: VmProfile,
    from_host: str,
    to_host: str,
) -> dict[str, float]:
    """Return new score state after moving a VM."""
    new_scores = dict(scores)
    new_scores[from_host] = new_scores.get(from_host, 0.0) - vm.weight
    new_scores[to_host] = new_scores.get(to_host, 0.0) + vm.weight
    return new_scores


def _imbalance(scores: dict[str, float]) -> float:
    """Compute imbalance as (max - min) of scores."""
    if len(scores) < 2:
        return 0.0
    values = list(scores.values())
    return max(values) - min(values)


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

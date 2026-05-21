"""Constraint checking for migration planning.

Validates that a proposed VM move does not violate Nova server group
placement rules.  All four Nova server group policy types are treated
as move-blocking:

* ``anti-affinity``: group members must be on separate hosts
* ``soft-anti-affinity``: best-effort spread; honored as a hint
* ``affinity``: group members must be on the same host
* ``soft-affinity``: best-effort co-location; honored as a hint

See https://docs.openstack.org/nova/latest/user/server-groups.html

Soft policies are currently treated with the same strictness as hard
ones - we will not knowingly break a soft rule during migration.  A
future change will plug soft rules into the planner as a weighted
penalty rather than a hard veto.

Additional constraints (NUMA, flavor extra specs) can be added here in
the future.
"""

from __future__ import annotations

from dataclasses import dataclass

from oslo_log import log as logging

from kronos.clients.nova import ComputeService, NovaClient
from kronos.engine.placement import PlacementGate
from kronos.engine.types import VmProfile

LOG = logging.getLogger(__name__)


ANTI_AFFINITY_POLICIES = frozenset({"anti-affinity", "soft-anti-affinity"})
AFFINITY_POLICIES = frozenset({"affinity", "soft-affinity"})


@dataclass(frozen=True)
class ServerGroup:
    """A single Nova server group, parsed from openstacksdk output."""

    group_id: str
    policy: str
    members: frozenset[str]


class ConstraintChecker:
    """Checks whether a proposed migration is safe.

    Current checks (all strict - even soft policies veto moves):
      - ``anti-affinity`` / ``soft-anti-affinity``: the destination host
        must not already hold another member of the group.
      - ``affinity`` / ``soft-affinity``: all other placed members of
        the group must already be on the destination host.

    Future:
      - NUMA topology
      - CPU feature flags
      - Flavor extra specs / traits
      - Promote soft rules to planner-side penalties instead of vetoes
    """

    def __init__(self, nova: NovaClient) -> None:
        self._nova = nova
        # Lazy-loaded cache; populated on first check() in a cycle.
        self._groups: list[ServerGroup] | None = None
        # Per-cycle nova-compute service map (host -> ComputeService).
        # The engine loop installs this every cycle via set_services();
        # when it is None, every destination is considered unavailable.
        self._services: dict[str, ComputeService] | None = None
        # Optional Nova-placement claims gate.  When installed, every
        # destination must also satisfy the VM's flavor claim against
        # the cached placement headroom.  Pluggable so spread, pack,
        # the evacuator, and the affinity enforcer all run through the
        # same check by virtue of using this checker.
        self._placement_gate: PlacementGate | None = None

    def set_placement_gate(self, gate: PlacementGate | None) -> None:
        """Install (or remove) the per-cycle placement claims gate.

        ``None`` (the default) skips the placement check entirely, which
        is the bit-for-bit pre-existing behaviour.  When set, the gate
        is consulted from :meth:`check` on every candidate move and
        from :meth:`commit_move` on every accepted move.
        """
        self._placement_gate = gate

    def commit_move(
        self,
        vm: VmProfile,
        from_host: str,
        to_host: str,
    ) -> None:
        """Notify the placement gate (if any) of an accepted move.

        Called by the planner, the affinity enforcer, and the
        evacuator right after they commit a move.  Updates the
        placement ledger so subsequent speculative candidates in the
        same cycle see truthful headroom.
        """
        if self._placement_gate is not None:
            self._placement_gate.commit_move(
                vm.instance_uuid, from_host, to_host,
            )

    def set_services(self, services: dict[str, ComputeService] | None) -> None:
        """Install per-cycle nova-compute service state.

        Called by the engine loop once per cycle.  When set, only hosts
        whose nova-compute service is ``state=up`` and ``status=enabled``
        (and not ``forced_down``) are accepted as live-migration
        destinations.  When ``None`` the checker fails closed - no host
        is treated as a valid destination.
        """
        self._services = services

    def is_host_available_destination(self, host: str) -> bool:
        """Return True if ``host`` can receive a live migration right now.

        Fails closed: when service state has not been installed for
        this cycle, or when ``host`` is missing from the installed
        map, the host is rejected.  A host is also rejected when its
        compute service is ``state=down``, ``status=disabled``, or
        ``forced_down=true``.
        """
        if self._services is None:
            return False
        svc = self._services.get(host)
        if svc is None:
            return False
        return svc.is_available_destination

    def check(
        self,
        vm: VmProfile,
        dest_host: str,
        vms_by_host: dict[str, list[VmProfile]],
    ) -> bool:
        """Return True if moving ``vm`` to ``dest_host`` is safe.

        :param vm: The VM being moved.
        :param dest_host: The proposed destination host.
        :param vms_by_host: Current VM placement (includes simulated moves).
        :returns: True if the move passes all constraint checks.
        """
        if not self.is_host_available_destination(dest_host):
            LOG.debug(
                "Host availability: VM %s cannot move to %s "
                "(nova-compute service unavailable).",
                vm.instance_uuid,
                dest_host,
            )
            return False

        if (
            self._placement_gate is not None
            and not self._placement_gate.is_destination_ok(
                vm.instance_uuid, dest_host,
            )
        ):
            return False

        groups = self._get_groups()
        if not groups:
            return True

        for group in groups:
            if vm.instance_uuid not in group.members:
                continue

            if (
                group.policy in ANTI_AFFINITY_POLICIES
                and not self._check_anti_affinity(
                    vm, group, dest_host, vms_by_host,
                )
            ):
                return False
            if (
                group.policy in AFFINITY_POLICIES
                and not self._check_affinity(
                    vm, group, dest_host, vms_by_host,
                )
            ):
                return False

        return True

    def _check_anti_affinity(
        self,
        vm: VmProfile,
        group: ServerGroup,
        dest_host: str,
        vms_by_host: dict[str, list[VmProfile]],
    ) -> bool:
        """Reject if another group member already lives on ``dest_host``."""
        dest_vms = {v.instance_uuid for v in vms_by_host.get(dest_host, [])}
        conflict = (group.members & dest_vms) - {vm.instance_uuid}
        if conflict:
            LOG.debug(
                "%s violation: VM %s cannot move to %s "
                "(group %s has members %s on that host).",
                group.policy,
                vm.instance_uuid,
                dest_host,
                group.group_id,
                conflict,
            )
            return False
        return True

    def _check_affinity(
        self,
        vm: VmProfile,
        group: ServerGroup,
        dest_host: str,
        vms_by_host: dict[str, list[VmProfile]],
    ) -> bool:
        """Reject if any placed group member is on a host other than ``dest_host``.

        A move is allowed when every other *currently placed* member of
        the group is already on ``dest_host``.  Members not visible in
        ``vms_by_host`` are ignored - Kronos only reasons about VMs in
        the aggregate under evaluation.
        """
        other_members = group.members - {vm.instance_uuid}
        if not other_members:
            return True

        # Build an index of placed member -> host for this group.
        placement: dict[str, str] = {}
        for host, vms in vms_by_host.items():
            for v in vms:
                if v.instance_uuid in other_members:
                    placement[v.instance_uuid] = host

        if not placement:
            return True

        off_host_members = {
            uuid for uuid, host in placement.items() if host != dest_host
        }
        if off_host_members:
            LOG.debug(
                "%s violation: VM %s cannot move to %s "
                "(group %s members %s are not on that host).",
                group.policy,
                vm.instance_uuid,
                dest_host,
                group.group_id,
                off_host_members,
            )
            return False
        return True

    def get_groups(self) -> list[ServerGroup]:
        """Fetch and cache relevant server groups from Nova.

        Shared with the affinity enforcer; both need the parsed group
        list and it is cheaper to fetch it once per cycle.
        """
        return self._get_groups()

    def _get_groups(self) -> list[ServerGroup]:
        if self._groups is not None:
            return self._groups

        self._groups = []
        try:
            raw_groups = self._nova.list_server_groups()
        except Exception:
            LOG.warning(
                "Failed to fetch server groups; skipping constraint checks.",
                exc_info=True,
            )
            return self._groups

        tracked = ANTI_AFFINITY_POLICIES | AFFINITY_POLICIES
        for group in raw_groups:
            group_policies = group.get("policies") or []
            if not isinstance(group_policies, list):
                continue
            matched = [p for p in group_policies if p in tracked]
            if not matched:
                continue
            # A group has at most one placement policy in practice;
            # pick the first tracked value for logging clarity.
            policy = str(matched[0])
            group_id = str(group.get("id", ""))
            raw_members = group.get("members") or []
            if not isinstance(raw_members, list):
                continue
            members = frozenset(str(m) for m in raw_members)
            if not members:
                continue
            self._groups.append(
                ServerGroup(
                    group_id=group_id,
                    policy=policy,
                    members=members,
                ),
            )

        anti = sum(1 for g in self._groups if g.policy in ANTI_AFFINITY_POLICIES)
        aff = sum(1 for g in self._groups if g.policy in AFFINITY_POLICIES)
        LOG.info(
            "Loaded %d server groups (%d anti-affinity, %d affinity).",
            len(self._groups), anti, aff,
        )
        return self._groups

    def invalidate_cache(self) -> None:
        """Clear cached server groups, service state, and placement gate.

        Call between engine cycles.  Server groups are re-fetched lazily
        on the next ``check()``.  Service state must be re-installed
        via :meth:`set_services` before the planner runs - until then
        the checker fails closed.  The placement gate (when in use) is
        cleared too; the engine loop re-installs a fresh ledger every
        cycle.
        """
        self._groups = None
        self._services = None
        if self._placement_gate is not None:
            self._placement_gate.invalidate()

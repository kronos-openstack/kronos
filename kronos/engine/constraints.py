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

from kronos.clients.nova import NovaClient
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
        """Clear cached server groups (call between engine cycles)."""
        self._groups = None

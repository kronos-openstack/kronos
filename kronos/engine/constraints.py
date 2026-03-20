"""Constraint checking for migration planning.

Validates that a proposed VM move does not violate Nova server group
anti-affinity rules.  Additional constraints (NUMA, flavor extra specs)
can be added here in future milestones.
"""

from __future__ import annotations

from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.engine.types import VmProfile

LOG = logging.getLogger(__name__)


class ConstraintChecker:
    """Checks whether a proposed migration is safe.

    Currently checks:
      - Server group anti-affinity: a VM must not land on a host
        that already has a member of the same anti-affinity group.

    Future:
      - NUMA topology
      - CPU feature flags
      - Flavor extra specs / traits
    """

    def __init__(self, nova: NovaClient) -> None:
        self._nova = nova
        # Lazy-loaded cache, populated on first check()
        self._anti_affinity_groups: dict[str, set[str]] | None = None

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
        return self._check_anti_affinity(vm, dest_host, vms_by_host)

    def _check_anti_affinity(
        self,
        vm: VmProfile,
        dest_host: str,
        vms_by_host: dict[str, list[VmProfile]],
    ) -> bool:
        """Check that the move does not violate anti-affinity rules."""
        groups = self._get_anti_affinity_groups()
        if not groups:
            return True

        # Find which anti-affinity groups this VM belongs to
        vm_groups = [
            (group_id, members)
            for group_id, members in groups.items()
            if vm.instance_uuid in members
        ]
        if not vm_groups:
            return True

        # Check if any member of the same group is already on dest_host
        dest_vms = {v.instance_uuid for v in vms_by_host.get(dest_host, [])}

        for group_id, members in vm_groups:
            conflict = members & dest_vms
            if conflict:
                LOG.debug(
                    "Anti-affinity violation: VM %s cannot move to %s "
                    "(group %s has members %s on that host).",
                    vm.instance_uuid,
                    dest_host,
                    group_id,
                    conflict,
                )
                return False

        return True

    def _get_anti_affinity_groups(self) -> dict[str, set[str]]:
        """Fetch and cache anti-affinity server groups from Nova.

        Returns a mapping of group_id -> set of member instance UUIDs
        for groups with an 'anti-affinity' policy.
        """
        if self._anti_affinity_groups is not None:
            return self._anti_affinity_groups

        self._anti_affinity_groups = {}
        try:
            groups = self._nova.list_server_groups()
        except Exception:
            LOG.warning(
                "Failed to fetch server groups; skipping anti-affinity checks.",
                exc_info=True,
            )
            return self._anti_affinity_groups

        for group in groups:
            group_policies = group.get("policies") or []
            if not isinstance(group_policies, list):
                continue
            if "anti-affinity" in group_policies:
                group_id = str(group.get("id", ""))
                raw_members = group.get("members") or []
                if not isinstance(raw_members, list):
                    continue
                members = {str(m) for m in raw_members}
                if members:
                    self._anti_affinity_groups[group_id] = members

        LOG.info(
            "Loaded %d anti-affinity server groups.",
            len(self._anti_affinity_groups),
        )
        return self._anti_affinity_groups

    def invalidate_cache(self) -> None:
        """Clear cached server groups (call between engine cycles)."""
        self._anti_affinity_groups = None

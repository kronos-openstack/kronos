"""Tests for the constraint checker."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kronos.clients.nova import ComputeService
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.types import VmProfile


def _vm(uuid: str, host: str = "h1") -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weights={"test": 0.1},
        sources={"test": "prometheus"},
    )


def _all_up_services(*hosts: str) -> dict[str, ComputeService]:
    """Permissive service map: every host is up + enabled."""
    return {
        h: ComputeService(
            host=h,
            binary="nova-compute",
            state="up",
            status="enabled",
        )
        for h in hosts
    }


@pytest.fixture()
def mock_nova() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def checker(mock_nova: MagicMock) -> ConstraintChecker:
    c = ConstraintChecker(mock_nova)
    # Server-group tests don't care about service state; install a
    # permissive map so the host-availability gate is a no-op.
    c.set_services(_all_up_services("h1", "h2", "h3"))
    return c


class TestAntiAffinity:
    def test_no_server_groups(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = []
        vm = _vm("vm-1")
        vms_by_host: dict[str, list[VmProfile]] = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is True

    def test_vm_not_in_any_group(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["anti-affinity"], "members": ["other-vm"]},
        ]
        vm = _vm("vm-1")
        assert checker.check(vm, "h2", {"h2": []}) is True

    def test_anti_affinity_blocks_move(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["anti-affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is False

    def test_anti_affinity_allows_different_group(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["anti-affinity"], "members": ["vm-1", "vm-3"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is True

    def test_soft_anti_affinity_blocks_move(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Soft anti-affinity groups are also blocked - we respect the hint."""
        mock_nova.list_server_groups.return_value = [
            {
                "id": "g1",
                "policies": ["soft-anti-affinity"],
                "members": ["vm-1", "vm-2"],
            },
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is False

    def test_cache_invalidation(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = []
        checker.check(_vm("vm-1"), "h2", {})
        assert mock_nova.list_server_groups.call_count == 1

        # Second call uses cache
        checker.check(_vm("vm-1"), "h2", {})
        assert mock_nova.list_server_groups.call_count == 1

        # invalidate_cache clears both server groups and the service
        # map; the engine re-installs services every cycle right after
        # invalidating, so the test mirrors that flow.
        checker.invalidate_cache()
        checker.set_services(_all_up_services("h1", "h2"))
        checker.check(_vm("vm-1"), "h2", {})
        assert mock_nova.list_server_groups.call_count == 2

    def test_server_group_fetch_failure(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """If fetching server groups fails, allow the move (fail-open)."""
        mock_nova.list_server_groups.side_effect = RuntimeError("API down")
        vm = _vm("vm-1")
        assert checker.check(vm, "h2", {}) is True

    def test_empty_dest_host(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["anti-affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1")
        assert checker.check(vm, "h2", {}) is True


class TestAntiAffinityMaxServerPerHost:
    """The Nova 2.64 ``max_server_per_host`` anti-affinity rule."""

    def test_cap_of_two_allows_one_existing_member(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """With cap=2, moving onto a host holding one member is allowed."""
        mock_nova.list_server_groups.return_value = [
            {
                "id": "g1",
                "policies": ["anti-affinity"],
                "members": ["vm-1", "vm-2", "vm-3"],
                "rules": {"max_server_per_host": 2},
            },
        ]
        vm = _vm("vm-1", "h1")
        # h2 already holds one member (vm-2); cap is 2, so vm-1 fits.
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}
        assert checker.check(vm, "h2", vms_by_host) is True

    def test_cap_of_two_blocks_at_capacity(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """With cap=2, moving onto a host already holding two is blocked."""
        mock_nova.list_server_groups.return_value = [
            {
                "id": "g1",
                "policies": ["anti-affinity"],
                "members": ["vm-1", "vm-2", "vm-3"],
                "rules": {"max_server_per_host": 2},
            },
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2"), _vm("vm-3", "h2")]}
        assert checker.check(vm, "h2", vms_by_host) is False

    def test_absent_rules_default_to_strict(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """No rules dict (pre-2.64 cloud) means cap=1 - strict spread."""
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["anti-affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}
        assert checker.check(vm, "h2", vms_by_host) is False

    @pytest.mark.parametrize("bad", [0, -3, "two", None, 1.5])
    def test_malformed_or_sub_one_rule_falls_back_to_strict(
        self, checker: ConstraintChecker, mock_nova: MagicMock, bad: object,
    ) -> None:
        """A non-integer or < 1 cap degrades to strict anti-affinity."""
        mock_nova.list_server_groups.return_value = [
            {
                "id": "g1",
                "policies": ["anti-affinity"],
                "members": ["vm-1", "vm-2"],
                "rules": {"max_server_per_host": bad},
            },
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}
        assert checker.check(vm, "h2", vms_by_host) is False


class TestAffinity:
    def test_affinity_allows_move_to_member_host(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Move is allowed when every other placed group member is on dest."""
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is True

    def test_affinity_blocks_move_to_other_host(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Move is blocked when another group member sits on a different host."""
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [], "h3": [_vm("vm-2", "h3")]}

        assert checker.check(vm, "h2", vms_by_host) is False

    def test_affinity_allows_move_when_other_members_not_placed(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Members outside the current aggregate are ignored - Kronos only
        reasons about VMs visible in ``vms_by_host``.
        """
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": []}  # vm-2 not visible in this aggregate

        assert checker.check(vm, "h2", vms_by_host) is True

    def test_affinity_allows_single_member_group(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["affinity"], "members": ["vm-1"]},
        ]
        vm = _vm("vm-1", "h1")
        assert checker.check(vm, "h2", {"h2": []}) is True

    def test_affinity_blocks_when_members_split_across_hosts(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """If other members are on two different hosts, no single dest works."""
        mock_nova.list_server_groups.return_value = [
            {
                "id": "g1",
                "policies": ["affinity"],
                "members": ["vm-1", "vm-2", "vm-3"],
            },
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")], "h3": [_vm("vm-3", "h3")]}

        # Neither h2 nor h3 has the full set of other members.
        assert checker.check(vm, "h2", vms_by_host) is False
        assert checker.check(vm, "h3", vms_by_host) is False

    def test_soft_affinity_blocks_move(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Soft affinity is also strictly enforced (may relax later)."""
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["soft-affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [], "h3": [_vm("vm-2", "h3")]}

        assert checker.check(vm, "h2", vms_by_host) is False

    def test_mixed_affinity_and_anti_affinity_membership(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """A VM in both group types must satisfy both."""
        mock_nova.list_server_groups.return_value = [
            {"id": "aff", "policies": ["affinity"], "members": ["vm-1", "vm-2"]},
            {
                "id": "anti",
                "policies": ["anti-affinity"],
                "members": ["vm-1", "vm-3"],
            },
        ]
        vm = _vm("vm-1", "h1")
        # h2 has vm-2 (affinity satisfied) but not vm-3 (anti-affinity OK).
        assert checker.check(vm, "h2", {"h2": [_vm("vm-2", "h2")]}) is True
        # h3 has vm-3 (anti-affinity violated) regardless of affinity.
        vms = {"h3": [_vm("vm-2", "h3"), _vm("vm-3", "h3")]}
        assert checker.check(vm, "h3", vms) is False


class TestServiceStateGate:
    """Host-availability gate: destination must be up + enabled."""

    def test_no_services_installed_fails_closed(
        self, mock_nova: MagicMock,
    ) -> None:
        """Without an installed service map, every destination is rejected."""
        c = ConstraintChecker(mock_nova)
        # Note: deliberately not calling set_services().
        mock_nova.list_server_groups.return_value = []
        assert c.is_host_available_destination("h1") is False
        assert c.check(_vm("vm-1"), "h1", {}) is False

    def test_host_missing_from_service_map_rejected(
        self, mock_nova: MagicMock,
    ) -> None:
        c = ConstraintChecker(mock_nova)
        c.set_services(_all_up_services("h1"))  # h2 missing
        assert c.is_host_available_destination("h2") is False

    def test_disabled_service_rejected_as_destination(
        self, mock_nova: MagicMock,
    ) -> None:
        c = ConstraintChecker(mock_nova)
        c.set_services({
            "h1": ComputeService(
                host="h1",
                binary="nova-compute",
                state="up",
                status="disabled",
            ),
        })
        assert c.is_host_available_destination("h1") is False

    def test_down_service_rejected_as_destination(
        self, mock_nova: MagicMock,
    ) -> None:
        c = ConstraintChecker(mock_nova)
        c.set_services({
            "h1": ComputeService(
                host="h1",
                binary="nova-compute",
                state="down",
                status="enabled",
            ),
        })
        assert c.is_host_available_destination("h1") is False

    def test_forced_down_rejected_as_destination(
        self, mock_nova: MagicMock,
    ) -> None:
        c = ConstraintChecker(mock_nova)
        c.set_services({
            "h1": ComputeService(
                host="h1",
                binary="nova-compute",
                state="up",
                status="enabled",
                forced_down=True,
            ),
        })
        assert c.is_host_available_destination("h1") is False

    def test_check_blocks_when_service_state_unfit(
        self, mock_nova: MagicMock,
    ) -> None:
        """Service-state gate runs before any server-group logic."""
        c = ConstraintChecker(mock_nova)
        c.set_services({
            "h1": ComputeService(
                host="h1",
                binary="nova-compute",
                state="up",
                status="enabled",
            ),
            "h2": ComputeService(
                host="h2",
                binary="nova-compute",
                state="up",
                status="disabled",
            ),
        })
        mock_nova.list_server_groups.return_value = []
        # No server group involvement; pure service-state veto.
        assert c.check(_vm("vm-1"), "h2", {}) is False

    def test_invalidate_cache_clears_services(
        self, mock_nova: MagicMock,
    ) -> None:
        c = ConstraintChecker(mock_nova)
        c.set_services(_all_up_services("h1", "h2"))
        assert c.is_host_available_destination("h1") is True

        c.invalidate_cache()
        assert c.is_host_available_destination("h1") is False

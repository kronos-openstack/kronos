"""Tests for the constraint checker."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


@pytest.fixture()
def mock_nova() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def checker(mock_nova: MagicMock) -> ConstraintChecker:
    return ConstraintChecker(mock_nova)


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
        """Soft anti-affinity groups are also blocked — we respect the hint."""
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

        # After invalidation, fetches again
        checker.invalidate_cache()
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
        """Members outside the current aggregate are ignored — Kronos only
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

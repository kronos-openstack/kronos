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
        weight=0.1,
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

    def test_affinity_group_ignored(
        self, checker: ConstraintChecker, mock_nova: MagicMock,
    ) -> None:
        """Only anti-affinity groups are checked, affinity is irrelevant."""
        mock_nova.list_server_groups.return_value = [
            {"id": "g1", "policies": ["affinity"], "members": ["vm-1", "vm-2"]},
        ]
        vm = _vm("vm-1", "h1")
        vms_by_host = {"h2": [_vm("vm-2", "h2")]}

        assert checker.check(vm, "h2", vms_by_host) is True

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

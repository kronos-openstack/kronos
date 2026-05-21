"""Tests for the pluggable placement claims gate."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kronos.clients.nova import ComputeService
from kronos.clients.placement import (
    RC_DISK_GB,
    RC_MEMORY_MB,
    RC_VCPU,
    ProviderSnapshot,
    ResourceInventory,
)
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.placement import FlavorFootprint, PlacementGate
from kronos.engine.types import VmProfile


def _vm(uuid: str, host: str = "h1") -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weights={"p1": 0.1},
        sources={"p1": "prometheus"},
    )


def _snap(
    host: str,
    *,
    vcpu_total: int = 16,
    vcpu_used: int = 0,
    vcpu_ratio: float = 1.0,
    mem_total: int = 32768,
    mem_used: int = 0,
    mem_ratio: float = 1.0,
    disk_total: int = 1000,
    disk_used: int = 0,
    disk_ratio: float = 1.0,
) -> ProviderSnapshot:
    return ProviderSnapshot(
        host=host,
        inventories={
            RC_VCPU: ResourceInventory(
                total=vcpu_total, allocation_ratio=vcpu_ratio,
            ),
            RC_MEMORY_MB: ResourceInventory(
                total=mem_total, allocation_ratio=mem_ratio,
            ),
            RC_DISK_GB: ResourceInventory(
                total=disk_total, allocation_ratio=disk_ratio,
            ),
        },
        usages={
            RC_VCPU: vcpu_used,
            RC_MEMORY_MB: mem_used,
            RC_DISK_GB: disk_used,
        },
    )


def _all_up(*hosts: str) -> dict[str, ComputeService]:
    return {
        h: ComputeService(
            host=h, binary="nova-compute", state="up", status="enabled",
        )
        for h in hosts
    }


class TestPlacementGateDirect:
    def test_disabled_gate_always_accepts(self) -> None:
        gate = PlacementGate(enabled=False)
        gate.set_snapshots({"h1": _snap("h1", vcpu_total=0)})
        gate.set_flavors({"v1": FlavorFootprint(vcpus=8)})
        assert gate.is_destination_ok("v1", "h1") is True

    def test_enabled_gate_rejects_when_no_headroom(self) -> None:
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({"h1": _snap("h1", vcpu_total=4, vcpu_used=4)})
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        assert gate.is_destination_ok("v1", "h1") is False

    def test_allocation_ratio_widens_capacity(self) -> None:
        # 4 vcpus * 4.0 ratio = 16 effective; 12 used leaves 4 free.
        gate = PlacementGate(enabled=True)
        gate.set_snapshots(
            {"h1": _snap("h1", vcpu_total=4, vcpu_used=12, vcpu_ratio=4.0)},
        )
        gate.set_flavors({"v1": FlavorFootprint(vcpus=4)})
        assert gate.is_destination_ok("v1", "h1") is True
        gate.set_flavors({"v2": FlavorFootprint(vcpus=5)})
        assert gate.is_destination_ok("v2", "h1") is False

    def test_unknown_dest_fails_closed(self) -> None:
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({"h1": _snap("h1")})
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        assert gate.is_destination_ok("v1", "h2") is False

    def test_unknown_vm_fails_open(self) -> None:
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({"h1": _snap("h1")})
        assert gate.is_destination_ok("v-unknown", "h1") is True

    def test_commit_move_updates_headroom(self) -> None:
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({
            "h1": _snap("h1", vcpu_total=8, vcpu_used=2),
            "h2": _snap("h2", vcpu_total=8, vcpu_used=7),
        })
        gate.set_flavors({"v1": FlavorFootprint(vcpus=2)})

        # h2 has 1 vcpu free; a 2-vcpu claim does not fit.
        assert gate.is_destination_ok("v1", "h2") is False

        # After we credit the source (no-op here, v1 is on no host
        # yet) and "free" via a hypothetical reverse move, h1 has
        # extra headroom.  Use commit_move to move v1 from h1 -> h2,
        # confirming h1 gains capacity and h2 loses it.
        gate.commit_move("v1", from_host="h1", to_host="h2")
        # h2 now has -1 vcpu effective free; v1 still rejected.
        assert gate.is_destination_ok("v1", "h2") is False
        # h1 has 2 more vcpus free than before (credited 2).
        # That doesn't change the boolean for v1 (already passing) but
        # we can verify by checking the headroom path: another VM with
        # a large claim now fits where it would not have before.
        gate.set_flavors({"v_big": FlavorFootprint(vcpus=8)})
        assert gate.is_destination_ok("v_big", "h1") is True

    def test_disk_ignored_by_default(self) -> None:
        """``account_disk=False`` (the default) skips DISK_GB.

        Ceph-backed clusters report identical pool capacity on every
        compute and do not re-claim DISK_GB on a shared-storage live
        migration; enforcing disk would over-reject.  The default
        leaves VCPU and MEMORY_MB checked but ignores DISK_GB.
        """
        gate = PlacementGate(enabled=True)  # account_disk defaults to False
        gate.set_snapshots(
            {"h1": _snap("h1", disk_total=10, disk_used=10)},  # 0 free
        )
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1, memory_mb=128, disk_gb=500)})
        # Disk overflows by 500, but the gate ignores disk so it
        # still accepts the move (vcpu+memory fit).
        assert gate.is_destination_ok("v1", "h1") is True

    def test_disk_enforced_when_opted_in(self) -> None:
        gate = PlacementGate(enabled=True, account_disk=True)
        gate.set_snapshots(
            {"h1": _snap("h1", disk_total=10, disk_used=10)},
        )
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1, memory_mb=128, disk_gb=500)})
        assert gate.is_destination_ok("v1", "h1") is False

    def test_disk_not_debited_when_ignored(self) -> None:
        """Commit-move must not touch DISK_GB while ``account_disk`` is off.

        Otherwise the ledger drifts and later candidates see stale
        disk figures.
        """
        gate = PlacementGate(enabled=True)  # account_disk=False
        gate.set_snapshots({
            "h1": _snap("h1", disk_total=100, disk_used=0),
            "h2": _snap("h2", disk_total=100, disk_used=0),
        })
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1, memory_mb=128, disk_gb=80)})

        gate.commit_move("v1", from_host="h1", to_host="h2")
        # disk_gb headroom must be unchanged on both sides because
        # disk accounting is off.
        assert gate._headroom["h1"].disk_gb == 100.0
        assert gate._headroom["h2"].disk_gb == 100.0
        # vcpu and memory should have moved.
        assert gate._headroom["h1"].vcpu > gate._headroom["h2"].vcpu

    def test_invalidate_clears_state(self) -> None:
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({"h1": _snap("h1")})
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        assert gate.is_destination_ok("v1", "h1") is True
        gate.invalidate()
        # After invalidation both snapshots and flavors are gone.
        # Re-install only the flavor so the missing-snapshot branch
        # (fail closed for unknown destination) is what we are
        # asserting.
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        assert gate.is_destination_ok("v1", "h1") is False


class TestConstraintCheckerPlumbing:
    """The gate is supposed to slot transparently into the existing
    constraint checker.  When installed it must veto bad destinations;
    when absent the checker must behave exactly as before.
    """

    def test_no_gate_keeps_legacy_behaviour(self) -> None:
        nova = MagicMock()
        nova.list_server_groups.return_value = []
        c = ConstraintChecker(nova)
        c.set_services(_all_up("h1", "h2"))
        # No placement gate installed; check passes.
        assert c.check(_vm("v1"), "h2", {}) is True

    def test_gate_vetoes_when_destination_too_tight(self) -> None:
        nova = MagicMock()
        nova.list_server_groups.return_value = []
        c = ConstraintChecker(nova)
        c.set_services(_all_up("h1", "h2"))

        gate = PlacementGate(enabled=True)
        gate.set_snapshots({
            "h1": _snap("h1"),
            "h2": _snap("h2", vcpu_total=2, vcpu_used=2),
        })
        gate.set_flavors({"v1": FlavorFootprint(vcpus=4)})
        c.set_placement_gate(gate)

        assert c.check(_vm("v1"), "h2", {}) is False
        # And the unconstrained host is fine.
        assert c.check(_vm("v1"), "h1", {}) is True

    def test_commit_move_via_checker_updates_gate(self) -> None:
        """The movers call constraints.commit_move - this should
        cascade to the gate so the next speculative move sees fresh
        headroom.  Pluggability check: the same wiring serves spread,
        pack, evacuator, and the affinity enforcer.
        """
        nova = MagicMock()
        nova.list_server_groups.return_value = []
        c = ConstraintChecker(nova)
        c.set_services(_all_up("h1", "h2"))

        gate = PlacementGate(enabled=True)
        gate.set_snapshots({
            "h1": _snap("h1", vcpu_total=8, vcpu_used=6),
            "h2": _snap("h2", vcpu_total=8, vcpu_used=2),
        })
        gate.set_flavors({
            "v1": FlavorFootprint(vcpus=4),
            "v2": FlavorFootprint(vcpus=4),
        })
        c.set_placement_gate(gate)

        # h2 has 6 vcpu free; both v1 and v2 fit before any move.
        assert c.check(_vm("v1"), "h2", {}) is True
        assert c.check(_vm("v2"), "h2", {}) is True

        # Plan accepts v1 -> h2.  Commit through the checker.
        c.commit_move(_vm("v1", host="h1"), from_host="h1", to_host="h2")
        # h2 now has 2 vcpu free; v2 (4 vcpu) no longer fits.
        assert c.check(_vm("v2"), "h2", {}) is False

    def test_invalidate_cache_clears_gate_too(self) -> None:
        nova = MagicMock()
        nova.list_server_groups.return_value = []
        c = ConstraintChecker(nova)
        c.set_services(_all_up("h1"))
        gate = PlacementGate(enabled=True)
        gate.set_snapshots({"h1": _snap("h1")})
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        c.set_placement_gate(gate)

        assert c.check(_vm("v1"), "h1", {}) is True
        c.invalidate_cache()
        # After invalidation both gate state and services are gone.
        # Re-install services + a flavor (but no snapshot) so the
        # placement check is the one that fires - destination has no
        # headroom snapshot, fails closed.
        c.set_services(_all_up("h1"))
        gate.set_flavors({"v1": FlavorFootprint(vcpus=1)})
        assert c.check(_vm("v1"), "h1", {}) is False


class TestProviderSnapshot:
    def test_headroom_applies_allocation_ratio(self) -> None:
        inv = ResourceInventory(total=8, reserved=1, allocation_ratio=2.0)
        snap = ProviderSnapshot(
            host="h1",
            inventories={RC_VCPU: inv},
            usages={RC_VCPU: 5},
        )
        # (8 - 1) * 2.0 - 5 = 9
        assert snap.headroom(RC_VCPU) == pytest.approx(9.0)

    def test_headroom_zero_for_missing_class(self) -> None:
        snap = ProviderSnapshot(host="h1")
        assert snap.headroom(RC_VCPU) == 0.0

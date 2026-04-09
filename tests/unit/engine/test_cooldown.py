"""Tests for the cooldown tracker."""

from __future__ import annotations

import time

from kronos.engine.cooldown import CooldownTracker


def _tracker(
    aggregate_cd: float = 600.0,
    instance_cd: float = 900.0,
) -> CooldownTracker:
    return CooldownTracker(
        aggregate_cooldown_seconds=aggregate_cd,
        instance_cooldown_seconds=instance_cd,
    )


class TestAggregateCooldown:
    def test_no_emission_not_cooling(self) -> None:
        tracker = _tracker()
        assert not tracker.is_aggregate_cooling("gpu")

    def test_recent_emission_is_cooling(self) -> None:
        tracker = _tracker(aggregate_cd=600.0)
        tracker.record_plan_emission("gpu", ["vm-1"])
        assert tracker.is_aggregate_cooling("gpu")

    def test_expired_emission_not_cooling(self) -> None:
        tracker = _tracker(aggregate_cd=300.0)
        tracker.record_plan_emission("gpu", ["vm-1"])
        tracker._aggregate_emissions["gpu"] = time.monotonic() - 400
        assert not tracker.is_aggregate_cooling("gpu")

    def test_different_aggregate_independent(self) -> None:
        tracker = _tracker()
        tracker.record_plan_emission("gpu", ["vm-1"])
        assert not tracker.is_aggregate_cooling("hpc")


class TestInstanceCooldown:
    def test_no_emission_not_cooling(self) -> None:
        tracker = _tracker()
        assert not tracker.is_instance_cooling("vm-1")

    def test_recent_emission_is_cooling(self) -> None:
        tracker = _tracker()
        tracker.record_plan_emission("gpu", ["vm-1", "vm-2"])
        assert tracker.is_instance_cooling("vm-1")
        assert tracker.is_instance_cooling("vm-2")

    def test_expired_emission_not_cooling(self) -> None:
        tracker = _tracker(instance_cd=300.0)
        tracker.record_plan_emission("gpu", ["vm-1"])
        tracker._instance_emissions["vm-1"] = time.monotonic() - 400
        assert not tracker.is_instance_cooling("vm-1")

    def test_unrelated_instance_not_cooling(self) -> None:
        tracker = _tracker()
        tracker.record_plan_emission("gpu", ["vm-1"])
        assert not tracker.is_instance_cooling("vm-99")


class TestUpdateFromResult:
    def test_sets_cooldown_for_passive_engine(self) -> None:
        tracker = _tracker()
        tracker.update_from_result("gpu", "vm-1", success=True)
        assert tracker.is_aggregate_cooling("gpu")
        assert tracker.is_instance_cooling("vm-1")

    def test_failure_still_sets_cooldown(self) -> None:
        tracker = _tracker()
        tracker.update_from_result("gpu", "vm-1", success=False)
        assert tracker.is_aggregate_cooling("gpu")
        assert tracker.is_instance_cooling("vm-1")

    def test_overwrites_existing_timestamp(self) -> None:
        tracker = _tracker()
        tracker._aggregate_emissions["gpu"] = time.monotonic() - 9999
        tracker.update_from_result("gpu", "vm-1", success=True)
        assert tracker.is_aggregate_cooling("gpu")


class TestCleanup:
    def test_removes_old_entries(self) -> None:
        tracker = _tracker()
        tracker._aggregate_emissions["old-agg"] = time.monotonic() - 5000
        tracker._instance_emissions["old-vm"] = time.monotonic() - 5000
        tracker.record_plan_emission("fresh-agg", ["fresh-vm"])

        tracker.cleanup_expired(max_age_seconds=3600.0)

        assert "old-agg" not in tracker._aggregate_emissions
        assert "old-vm" not in tracker._instance_emissions
        assert "fresh-agg" in tracker._aggregate_emissions
        assert "fresh-vm" in tracker._instance_emissions

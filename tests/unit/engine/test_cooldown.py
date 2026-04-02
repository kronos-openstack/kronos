"""Tests for the cooldown tracker."""

from __future__ import annotations

import time
from datetime import timedelta

from kronos.engine.cooldown import CooldownTracker


class TestPolicyCooldown:
    def test_no_emission_not_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        assert not tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))

    def test_recent_emission_is_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1"])
        assert tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))

    def test_expired_emission_not_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1"])
        # Simulate time passing
        tracker._policy_emissions["cpu-spread"] = time.monotonic() - 700
        assert not tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))

    def test_different_policy_independent(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1"])
        assert not tracker.is_policy_cooling("mem-spread", timedelta(minutes=10))


class TestInstanceCooldown:
    def test_no_emission_not_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        assert not tracker.is_instance_cooling("vm-1")

    def test_recent_emission_is_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1", "vm-2"])
        assert tracker.is_instance_cooling("vm-1")
        assert tracker.is_instance_cooling("vm-2")

    def test_expired_emission_not_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=300.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1"])
        tracker._instance_emissions["vm-1"] = time.monotonic() - 400
        assert not tracker.is_instance_cooling("vm-1")

    def test_unrelated_instance_not_cooling(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.record_plan_emission("cpu-spread", ["vm-1"])
        assert not tracker.is_instance_cooling("vm-99")


class TestUpdateFromResult:
    def test_sets_cooldown_for_passive_engine(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.update_from_result("cpu-spread", "vm-1", success=True)
        assert tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))
        assert tracker.is_instance_cooling("vm-1")

    def test_failure_still_sets_cooldown(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker.update_from_result("cpu-spread", "vm-1", success=False)
        assert tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))
        assert tracker.is_instance_cooling("vm-1")

    def test_overwrites_existing_timestamp(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        # Set old emission
        tracker._policy_emissions["cpu-spread"] = time.monotonic() - 9999
        tracker.update_from_result("cpu-spread", "vm-1", success=True)
        # Should now be cooling again
        assert tracker.is_policy_cooling("cpu-spread", timedelta(minutes=10))


class TestCleanup:
    def test_removes_old_entries(self) -> None:
        tracker = CooldownTracker(instance_cooldown_seconds=900.0)
        tracker._policy_emissions["old-policy"] = time.monotonic() - 5000
        tracker._instance_emissions["old-vm"] = time.monotonic() - 5000
        tracker.record_plan_emission("fresh-policy", ["fresh-vm"])

        tracker.cleanup_expired(max_age_seconds=3600.0)

        assert "old-policy" not in tracker._policy_emissions
        assert "old-vm" not in tracker._instance_emissions
        assert "fresh-policy" in tracker._policy_emissions
        assert "fresh-vm" in tracker._instance_emissions

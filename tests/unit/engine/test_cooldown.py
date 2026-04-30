"""Tests for the cooldown tracker."""

from __future__ import annotations

import math
import time

from kronos.engine.cooldown import QUARANTINE_FOREVER, CooldownTracker


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

    def test_preserves_forever_quarantine(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-forever", QUARANTINE_FOREVER)
        tracker.quarantine_instance("vm-short", 10.0)
        tracker._instance_quarantine["vm-short"] = time.monotonic() - 1

        tracker.cleanup_expired(max_age_seconds=3600.0)

        assert tracker._instance_quarantine["vm-forever"] == math.inf
        assert "vm-short" not in tracker._instance_quarantine


class TestQuarantine:
    def test_fresh_instance_not_quarantined(self) -> None:
        tracker = _tracker()
        assert not tracker.is_instance_quarantined("vm-1")

    def test_recent_quarantine_is_active(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", 600.0)
        assert tracker.is_instance_quarantined("vm-1")

    def test_forever_quarantine_stays_active(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", QUARANTINE_FOREVER)
        assert tracker.is_instance_quarantined("vm-1")
        assert tracker._instance_quarantine["vm-1"] == math.inf

    def test_expired_quarantine_cleared_on_read(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", 60.0)
        tracker._instance_quarantine["vm-1"] = time.monotonic() - 1
        assert not tracker.is_instance_quarantined("vm-1")
        assert "vm-1" not in tracker._instance_quarantine

    def test_zero_or_negative_duration_is_noop(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", 0)
        tracker.quarantine_instance("vm-2", -5)
        assert not tracker.is_instance_quarantined("vm-1")
        assert not tracker.is_instance_quarantined("vm-2")

    def test_forever_sentinel_beats_zero(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", QUARANTINE_FOREVER)
        assert tracker.is_instance_quarantined("vm-1")

    def test_re_quarantine_extends(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", 10.0)
        first_expiry = tracker._instance_quarantine["vm-1"]
        tracker.quarantine_instance("vm-1", 3600.0)
        second_expiry = tracker._instance_quarantine["vm-1"]
        assert second_expiry > first_expiry

    def test_forever_quarantine_overrides_existing_timed(self) -> None:
        tracker = _tracker()
        tracker.quarantine_instance("vm-1", 10.0)
        tracker.quarantine_instance("vm-1", QUARANTINE_FOREVER)
        assert tracker._instance_quarantine["vm-1"] == math.inf


class TestSeed:
    def test_seed_aggregate_cooldown_activates_it(self) -> None:
        tracker = _tracker(aggregate_cd=600.0)
        tracker.seed_aggregate_cooldown("gpu", 120.0)
        assert tracker.is_aggregate_cooling("gpu")

    def test_seed_aggregate_cooldown_clamped_to_configured(self) -> None:
        tracker = _tracker(aggregate_cd=600.0)
        tracker.seed_aggregate_cooldown("gpu", 9999.0)
        # Still cooling, but the oldest possible - effective remaining
        # is configured.
        assert tracker.is_aggregate_cooling("gpu")
        elapsed = time.monotonic() - tracker._aggregate_emissions["gpu"]
        assert elapsed < 1.0  # emission ~= now

    def test_seed_aggregate_zero_is_noop(self) -> None:
        tracker = _tracker()
        tracker.seed_aggregate_cooldown("gpu", 0.0)
        assert not tracker.is_aggregate_cooling("gpu")

    def test_seed_aggregate_negative_is_noop(self) -> None:
        tracker = _tracker()
        tracker.seed_aggregate_cooldown("gpu", -5.0)
        assert not tracker.is_aggregate_cooling("gpu")

    def test_seed_instance_cooldown_activates_it(self) -> None:
        tracker = _tracker(instance_cd=900.0)
        tracker.seed_instance_cooldown("vm-1", 300.0)
        assert tracker.is_instance_cooling("vm-1")

    def test_seed_instance_cooldown_zero_is_noop(self) -> None:
        tracker = _tracker()
        tracker.seed_instance_cooldown("vm-1", 0)
        assert not tracker.is_instance_cooling("vm-1")

    def test_seed_quarantine_timed(self) -> None:
        tracker = _tracker()
        tracker.seed_instance_quarantine("vm-1", 1800.0)
        assert tracker.is_instance_quarantined("vm-1")

    def test_seed_quarantine_forever(self) -> None:
        tracker = _tracker()
        tracker.seed_instance_quarantine("vm-1", QUARANTINE_FOREVER)
        assert tracker._instance_quarantine["vm-1"] == math.inf

    def test_seed_quarantine_zero_is_noop(self) -> None:
        tracker = _tracker()
        tracker.seed_instance_quarantine("vm-1", 0)
        assert not tracker.is_instance_quarantined("vm-1")

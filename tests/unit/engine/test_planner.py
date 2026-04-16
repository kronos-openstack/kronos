"""Tests for the combined-scoring migration planner."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from kronos.engine._sim import (
    combined_imbalance,
    group_vms_by_host,
    simulate_move,
)
from kronos.engine.planner import Planner
from kronos.engine.types import HostScore, MigrationPlan, PolicyResult, VmProfile
from kronos.policies.models import PolicyConfig, PolicyMode


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "test-policy",
        "mode": "spread",
        "weight": 1.0,
        "imbalance_query": "test_metric",
        "threshold": 0.15,
        "max_migrations_per_cycle": 5,
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _vm(
    uuid: str,
    host: str,
    weights: dict[str, float],
) -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weights=dict(weights),
        sources={k: "prometheus" for k in weights},
    )


def _policy_result(
    policy_name: str,
    host_scores: dict[str, float],
    mode: PolicyMode = PolicyMode.SPREAD,
    skipped: bool = False,
) -> PolicyResult:
    scores = [
        HostScore(host=h, raw_score=s, normalized_score=0.0)
        for h, s in host_scores.items()
    ]
    if skipped:
        return PolicyResult(
            policy_name=policy_name,
            mode=mode,
            host_scores=[],
            imbalance=0.0,
            imbalance_detected=False,
            timestamp=datetime.now(tz=UTC),
            evaluation_duration_ms=1.0,
            skipped=True,
            skip_reason="test",
        )
    imbalance = (
        max(host_scores.values()) - min(host_scores.values())
        if len(host_scores) >= 2 else 0.0
    )
    return PolicyResult(
        policy_name=policy_name,
        mode=mode,
        host_scores=scores,
        imbalance=imbalance,
        imbalance_detected=imbalance > 0,
        timestamp=datetime.now(tz=UTC),
        evaluation_duration_ms=1.0,
    )


@pytest.fixture()
def allow_all_constraints() -> MagicMock:
    checker = MagicMock()
    checker.check.return_value = True
    return checker


@pytest.fixture()
def planner(allow_all_constraints: MagicMock) -> Planner:
    return Planner(allow_all_constraints)


# --- Helper function tests ---


class TestHelpers:
    def testcombined_imbalance_empty(self) -> None:
        assert combined_imbalance({}, []) == 0.0

    def testcombined_imbalance_single_policy(self) -> None:
        scores = {"p1": {"h1": 0.8, "h2": 0.3}}
        policies = [_make_policy(name="p1", weight=1.0)]
        assert combined_imbalance(scores, policies) == pytest.approx(0.5)

    def testcombined_imbalance_two_policies(self) -> None:
        scores = {
            "cpu": {"h1": 0.4, "h2": 0.2},     # imbalance 0.2
            "mem": {"h1": 0.9, "h2": 0.3},     # imbalance 0.6
        }
        policies = [
            _make_policy(name="cpu", weight=0.3),
            _make_policy(name="mem", weight=0.7),
        ]
        # 0.3 * 0.2 + 0.7 * 0.6 = 0.06 + 0.42 = 0.48
        assert combined_imbalance(scores, policies) == pytest.approx(0.48)

    def testsimulate_move_affects_all_policies(self) -> None:
        scores = {
            "cpu": {"h1": 0.5, "h2": 0.2},
            "mem": {"h1": 0.8, "h2": 0.3},
        }
        vm = _vm("v1", "h1", {"cpu": 0.1, "mem": 0.2})
        new = simulate_move(scores, vm, "h1", "h2")
        assert new["cpu"]["h1"] == pytest.approx(0.4)
        assert new["cpu"]["h2"] == pytest.approx(0.3)
        assert new["mem"]["h1"] == pytest.approx(0.6)
        assert new["mem"]["h2"] == pytest.approx(0.5)
        # original unchanged
        assert scores["cpu"]["h1"] == pytest.approx(0.5)

    def testgroup_vms_by_host(self) -> None:
        profiles = {
            "v1": _vm("v1", "h1", {"p1": 0.1}),
            "v2": _vm("v2", "h1", {"p1": 0.2}),
            "v3": _vm("v3", "h2", {"p1": 0.3}),
        }
        grouped = group_vms_by_host(profiles)
        assert len(grouped["h1"]) == 2
        assert len(grouped["h2"]) == 1


# --- Single-policy spread tests (simplest case) ---


class TestSpreadSinglePolicy:
    def test_no_vms_returns_empty_plan(self, planner: Planner) -> None:
        policy = _make_policy()
        results = [_policy_result("test-policy", {"h1": 0.8, "h2": 0.3})]
        plan = planner.plan("agg", [policy], results, {})
        assert plan.migration_count == 0

    def test_balanced_hosts_no_migration(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.15)
        results = [_policy_result("test-policy", {"h1": 0.50, "h2": 0.48})]
        profiles = {"v1": _vm("v1", "h1", {"test-policy": 0.05})}
        plan = planner.plan("agg", [policy], results, profiles)
        assert plan.migration_count == 0

    def test_imbalanced_hosts_proposes_migration(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.10)
        results = [_policy_result("test-policy", {"h1": 0.8, "h2": 0.2})]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.2}),
            "v2": _vm("v2", "h2", {"test-policy": 0.1}),
        }
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.migration_count >= 1
        step = plan.steps[0]
        assert step.from_host == "h1"
        assert step.to_host == "h2"
        assert step.instance_uuid == "v1"

    def test_respects_max_migrations(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.01, max_migrations_per_cycle=1)
        results = [_policy_result("test-policy", {"h1": 0.9, "h2": 0.1})]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.1}),
            "v2": _vm("v2", "h1", {"test-policy": 0.1}),
            "v3": _vm("v3", "h1", {"test-policy": 0.1}),
        }
        plan = planner.plan("agg", [policy], results, profiles)
        assert plan.migration_count == 1

    def test_projected_imbalance_decreases(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.10)
        results = [_policy_result("test-policy", {"h1": 0.8, "h2": 0.2})]
        profiles = {"v1": _vm("v1", "h1", {"test-policy": 0.2})}
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.projected_imbalance < plan.initial_imbalance

    def test_stops_when_balanced(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.15, max_migrations_per_cycle=10)
        results = [_policy_result("test-policy", {"h1": 0.6, "h2": 0.3})]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.05}),
            "v2": _vm("v2", "h1", {"test-policy": 0.05}),
            "v3": _vm("v3", "h1", {"test-policy": 0.05}),
        }
        plan = planner.plan("agg", [policy], results, profiles)
        assert plan.projected_imbalance <= policy.threshold

    def test_constraint_blocks_move(self) -> None:
        blocker = MagicMock()
        blocker.check.return_value = False
        planner = Planner(blocker)

        policy = _make_policy(threshold=0.01)
        results = [_policy_result("test-policy", {"h1": 0.9, "h2": 0.1})]
        profiles = {"v1": _vm("v1", "h1", {"test-policy": 0.2})}
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.migration_count == 0

    def test_skipped_policy_excluded(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.10)
        results = [_policy_result("test-policy", {}, skipped=True)]
        profiles = {"v1": _vm("v1", "h1", {"test-policy": 0.2})}
        plan = planner.plan("agg", [policy], results, profiles)
        assert plan.migration_count == 0


# --- Combined-scoring tests (the new behavior) ---


class TestCombinedScoring:
    def test_move_rejected_if_breaches_other_policy(
        self, planner: Planner,
    ) -> None:
        """A VM that fixes memory imbalance but would push CPU above
        threshold is rejected, even if the combined score improves."""
        cpu = _make_policy(name="cpu", weight=0.3, threshold=0.15)
        mem = _make_policy(name="mem", weight=0.7, threshold=0.15)

        # CPU is near threshold already on h2; moving a CPU-heavy VM to h2
        # would push CPU over 0.15 imbalance.
        cpu_scores = {"h1": 0.15, "h2": 0.05}        # cpu imbalance 0.10
        mem_scores = {"h1": 0.80, "h2": 0.20}        # mem imbalance 0.60

        results = [
            _policy_result("cpu", cpu_scores),
            _policy_result("mem", mem_scores),
        ]

        # Only one VM — a heavy-cpu + heavy-mem one on h1. Moving it
        # drops mem imbalance but inverts cpu imbalance to 0.25 > 0.15.
        profiles = {
            "v1": _vm("v1", "h1", {"cpu": 0.20, "mem": 0.50}),
        }
        plan = planner.plan("agg", [cpu, mem], results, profiles)

        # The move breaches cpu threshold → rejected
        assert plan.migration_count == 0

    def test_move_accepted_if_all_policies_stay_within_threshold(
        self, planner: Planner,
    ) -> None:
        """A moderate move that improves the combined score and keeps
        every individual policy below its threshold is accepted."""
        cpu = _make_policy(name="cpu", weight=0.5, threshold=0.50)
        mem = _make_policy(name="mem", weight=0.5, threshold=0.20)

        cpu_scores = {"h1": 0.40, "h2": 0.10}        # 0.30 — above threshold
        mem_scores = {"h1": 0.50, "h2": 0.10}        # 0.40 — above threshold

        results = [
            _policy_result("cpu", cpu_scores),
            _policy_result("mem", mem_scores),
        ]
        profiles = {
            "v1": _vm("v1", "h1", {"cpu": 0.15, "mem": 0.20}),
        }
        plan = planner.plan("agg", [cpu, mem], results, profiles)

        assert plan.migration_count == 1
        step = plan.steps[0]
        assert step.from_host == "h1"
        assert step.to_host == "h2"

    def test_stops_when_all_policies_happy(self, planner: Planner) -> None:
        cpu = _make_policy(
            name="cpu", weight=0.5, threshold=0.15, max_migrations_per_cycle=10,
        )
        mem = _make_policy(
            name="mem", weight=0.5, threshold=0.15, max_migrations_per_cycle=10,
        )
        # Tiny imbalances that both fit under their thresholds already
        results = [
            _policy_result("cpu", {"h1": 0.10, "h2": 0.05}),
            _policy_result("mem", {"h1": 0.10, "h2": 0.05}),
        ]
        profiles = {"v1": _vm("v1", "h1", {"cpu": 0.02, "mem": 0.02})}
        plan = planner.plan("agg", [cpu, mem], results, profiles)
        assert plan.migration_count == 0


# --- Pack tests ---


class TestPackPlanner:
    def test_basic_packing(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
        )
        results = [
            _policy_result(
                "test-policy",
                {"h1": 0.2, "h2": 0.5, "h3": 0.5},
                mode=PolicyMode.PACK,
            ),
        ]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.1}),
            "v2": _vm("v2", "h1", {"test-policy": 0.05}),
        }
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.migration_count >= 1
        for step in plan.steps:
            assert step.from_host == "h1"

    def test_capacity_threshold_respected(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.55,
        )
        results = [
            _policy_result(
                "test-policy",
                {"h1": 0.1, "h2": 0.50},
                mode=PolicyMode.PACK,
            ),
        ]
        profiles = {
            # 0.50 + 0.10 = 0.60 > 0.55 cap → refused
            "v1": _vm("v1", "h1", {"test-policy": 0.1}),
        }
        plan = planner.plan("agg", [policy], results, profiles)
        assert plan.migration_count == 0

    def test_draining_hosts_not_destinations(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
        )
        results = [
            _policy_result(
                "test-policy",
                {"h1": 0.1, "h2": 0.2, "h3": 0.5},
                mode=PolicyMode.PACK,
            ),
        ]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.05}),
            "v2": _vm("v2", "h2", {"test-policy": 0.05}),
        }
        plan = planner.plan("agg", [policy], results, profiles)

        for step in plan.steps:
            assert step.to_host != "h1"

    def test_empty_host_fully_drained(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
            max_migrations_per_cycle=10,
        )
        results = [
            _policy_result(
                "test-policy",
                {"h1": 0.1, "h2": 0.3},
                mode=PolicyMode.PACK,
            ),
        ]
        profiles = {
            "v1": _vm("v1", "h1", {"test-policy": 0.05}),
            "v2": _vm("v2", "h1", {"test-policy": 0.03}),
        }
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.migration_count == 2
        for step in plan.steps:
            assert step.from_host == "h1"
            assert step.to_host == "h2"

    def test_biggest_vm_moved_first(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
            max_migrations_per_cycle=1,
        )
        results = [
            _policy_result(
                "test-policy",
                {"h1": 0.1, "h2": 0.3},
                mode=PolicyMode.PACK,
            ),
        ]
        profiles = {
            "small": _vm("small", "h1", {"test-policy": 0.01}),
            "big": _vm("big", "h1", {"test-policy": 0.05}),
        }
        plan = planner.plan("agg", [policy], results, profiles)

        assert plan.migration_count == 1
        assert plan.steps[0].instance_uuid == "big"


class TestMigrationPlan:
    def test_migration_count_property(self) -> None:
        plan = MigrationPlan(aggregate="agg")
        assert plan.migration_count == 0

"""Tests for the migration planner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kronos.engine.planner import Planner, _group_vms_by_host, _imbalance, _simulate_move
from kronos.engine.types import HostScore, MigrationPlan, VmProfile
from kronos.policies.models import PolicyConfig


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "test-policy",
        "mode": "spread",
        "aggregate": "test-agg",
        "imbalance_query": "test_metric",
        "threshold": 0.15,
        "cooldown": "10m",
        "max_migrations_per_cycle": 5,
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _vm(uuid: str, host: str, weight: float) -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weight=weight,
    )


def _host_score(host: str, score: float) -> HostScore:
    return HostScore(host=host, raw_score=score, normalized_score=0.0)


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
    def test_imbalance_empty(self) -> None:
        assert _imbalance({}) == 0.0

    def test_imbalance_single(self) -> None:
        assert _imbalance({"h1": 0.5}) == 0.0

    def test_imbalance_two_hosts(self) -> None:
        assert _imbalance({"h1": 0.8, "h2": 0.3}) == pytest.approx(0.5)

    def test_simulate_move(self) -> None:
        scores = {"h1": 0.8, "h2": 0.3}
        vm = _vm("v1", "h1", 0.2)
        result = _simulate_move(scores, vm, "h1", "h2")
        assert result["h1"] == pytest.approx(0.6)
        assert result["h2"] == pytest.approx(0.5)
        # Original unchanged
        assert scores["h1"] == pytest.approx(0.8)

    def test_group_vms_by_host(self) -> None:
        profiles = {
            "v1": _vm("v1", "h1", 0.1),
            "v2": _vm("v2", "h1", 0.2),
            "v3": _vm("v3", "h2", 0.3),
        }
        grouped = _group_vms_by_host(profiles)
        assert len(grouped["h1"]) == 2
        assert len(grouped["h2"]) == 1


# --- Spread planner tests ---


class TestSpreadPlanner:
    def test_no_vms_returns_empty_plan(self, planner: Planner) -> None:
        policy = _make_policy()
        scores = [_host_score("h1", 0.8), _host_score("h2", 0.3)]
        plan = planner.plan(policy, scores, {})
        assert plan.migration_count == 0

    def test_single_host_returns_empty_plan(self, planner: Planner) -> None:
        policy = _make_policy()
        scores = [_host_score("h1", 0.8)]
        profiles = {"v1": _vm("v1", "h1", 0.1)}
        plan = planner.plan(policy, scores, profiles)
        assert plan.migration_count == 0

    def test_balanced_hosts_no_migration(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.15)
        scores = [_host_score("h1", 0.50), _host_score("h2", 0.48)]
        profiles = {"v1": _vm("v1", "h1", 0.05)}
        plan = planner.plan(policy, scores, profiles)
        assert plan.migration_count == 0

    def test_imbalanced_hosts_proposes_migration(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.10)
        scores = [_host_score("h1", 0.8), _host_score("h2", 0.2)]
        profiles = {
            "v1": _vm("v1", "h1", 0.2),
            "v2": _vm("v2", "h2", 0.1),
        }
        plan = planner.plan(policy, scores, profiles)

        assert plan.migration_count >= 1
        step = plan.steps[0]
        assert step.from_host == "h1"
        assert step.to_host == "h2"
        assert step.instance_uuid == "v1"

    def test_respects_max_migrations(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.01, max_migrations_per_cycle=1)
        scores = [_host_score("h1", 0.9), _host_score("h2", 0.1)]
        profiles = {
            "v1": _vm("v1", "h1", 0.1),
            "v2": _vm("v2", "h1", 0.1),
            "v3": _vm("v3", "h1", 0.1),
        }
        plan = planner.plan(policy, scores, profiles)
        assert plan.migration_count == 1

    def test_projected_imbalance_decreases(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.10)
        scores = [_host_score("h1", 0.8), _host_score("h2", 0.2)]
        profiles = {"v1": _vm("v1", "h1", 0.2)}
        plan = planner.plan(policy, scores, profiles)

        assert plan.projected_imbalance < plan.initial_imbalance

    def test_stops_when_balanced(self, planner: Planner) -> None:
        policy = _make_policy(threshold=0.15, max_migrations_per_cycle=10)
        scores = [_host_score("h1", 0.6), _host_score("h2", 0.3)]
        profiles = {
            "v1": _vm("v1", "h1", 0.05),
            "v2": _vm("v2", "h1", 0.05),
            "v3": _vm("v3", "h1", 0.05),
        }
        plan = planner.plan(policy, scores, profiles)

        # Should stop before max because balance is achieved
        assert plan.projected_imbalance <= policy.threshold

    def test_constraint_blocks_move(self) -> None:
        blocker = MagicMock()
        blocker.check.return_value = False
        planner = Planner(blocker)

        policy = _make_policy(threshold=0.01)
        scores = [_host_score("h1", 0.9), _host_score("h2", 0.1)]
        profiles = {"v1": _vm("v1", "h1", 0.2)}
        plan = planner.plan(policy, scores, profiles)

        assert plan.migration_count == 0


# --- Pack planner tests ---


class TestPackPlanner:
    def test_basic_packing(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
        )
        # h1 is cold (0.2), h2 is warm (0.5), h3 is warm (0.5)
        scores = [
            _host_score("h1", 0.2),
            _host_score("h2", 0.5),
            _host_score("h3", 0.5),
        ]
        profiles = {
            "v1": _vm("v1", "h1", 0.1),
            "v2": _vm("v2", "h1", 0.05),
        }
        plan = planner.plan(policy, scores, profiles)

        assert plan.migration_count >= 1
        # VMs should move away from the coldest host
        for step in plan.steps:
            assert step.from_host == "h1"

    def test_capacity_threshold_respected(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.55,
        )
        # h1 cold (0.1), h2 already at 0.50
        scores = [_host_score("h1", 0.1), _host_score("h2", 0.50)]
        profiles = {
            "v1": _vm("v1", "h1", 0.1),  # 0.50 + 0.1 = 0.60 > 0.55 threshold
        }
        plan = planner.plan(policy, scores, profiles)
        # Should not move because dest would exceed capacity
        assert plan.migration_count == 0

    def test_draining_hosts_not_destinations(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
        )
        # h1 is coldest, h2 is next coldest, h3 is warmest
        scores = [
            _host_score("h1", 0.1),
            _host_score("h2", 0.2),
            _host_score("h3", 0.5),
        ]
        profiles = {
            "v1": _vm("v1", "h1", 0.05),
            "v2": _vm("v2", "h2", 0.05),
        }
        plan = planner.plan(policy, scores, profiles)

        # h1 is draining, so h2's VMs should go to h3, not h1
        for step in plan.steps:
            assert step.to_host != "h1"

    def test_empty_host_fully_drained(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
            max_migrations_per_cycle=10,
        )
        scores = [_host_score("h1", 0.1), _host_score("h2", 0.3)]
        profiles = {
            "v1": _vm("v1", "h1", 0.05),
            "v2": _vm("v2", "h1", 0.03),
        }
        plan = planner.plan(policy, scores, profiles)

        assert plan.migration_count == 2
        for step in plan.steps:
            assert step.from_host == "h1"
            assert step.to_host == "h2"

    def test_respects_max_migrations(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
            max_migrations_per_cycle=1,
        )
        scores = [_host_score("h1", 0.1), _host_score("h2", 0.3)]
        profiles = {
            "v1": _vm("v1", "h1", 0.05),
            "v2": _vm("v2", "h1", 0.03),
        }
        plan = planner.plan(policy, scores, profiles)
        assert plan.migration_count == 1

    def test_biggest_vm_moved_first(self, planner: Planner) -> None:
        policy = _make_policy(
            mode="pack",
            capacity_query="cap",
            capacity_threshold=0.90,
            max_migrations_per_cycle=1,
        )
        scores = [_host_score("h1", 0.1), _host_score("h2", 0.3)]
        profiles = {
            "small": _vm("small", "h1", 0.01),
            "big": _vm("big", "h1", 0.05),
        }
        plan = planner.plan(policy, scores, profiles)

        assert plan.migration_count == 1
        assert plan.steps[0].instance_uuid == "big"


class TestMigrationPlan:
    def test_migration_count_property(self) -> None:
        plan = MigrationPlan(policy_name="test", aggregate="agg")
        assert plan.migration_count == 0

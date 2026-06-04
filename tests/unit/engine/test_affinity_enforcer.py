"""Tests for the affinity enforcer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from kronos.engine.affinity_enforcer import (
    AffinityEnforcer,
    _anti_affinity_offenders,
    _detect_violations,
)
from kronos.engine.constraints import ConstraintChecker, ServerGroup
from kronos.engine.types import (
    HostScore,
    MigrationPhase,
    PolicyResult,
    VmProfile,
)
from kronos.policies.models import PolicyConfig, PolicyMode


def _policy(name: str = "mem", weight: float = 1.0, threshold: float = 0.2) -> PolicyConfig:
    return PolicyConfig(
        name=name,
        mode=PolicyMode.SPREAD,
        weight=weight,
        imbalance_query="x",
        threshold=threshold,
    )


def _vm(uuid: str, host: str, weights: dict[str, float] | None = None) -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weights=weights or {"mem": 0.1},
        sources={"mem": "prometheus"},
    )


def _policy_result(
    policy_name: str,
    host_scores: list[HostScore],
    imbalance: float = 0.0,
) -> PolicyResult:
    return PolicyResult(
        policy_name=policy_name,
        mode=PolicyMode.SPREAD,
        host_scores=host_scores,
        imbalance=imbalance,
        imbalance_detected=imbalance > 0.15,
        timestamp=datetime.now(tz=UTC),
        evaluation_duration_ms=1.0,
    )


@pytest.fixture()
def constraints() -> MagicMock:
    """ConstraintChecker mock wired with a custom get_groups() return."""
    return MagicMock(spec=ConstraintChecker)


class TestDetectViolations:
    def test_no_groups_no_violations(self) -> None:
        assert _detect_violations([], {"h1": [_vm("a", "h1")]}) == []

    def test_single_visible_member_cannot_violate(self) -> None:
        group = ServerGroup(
            group_id="g",
            policy="anti-affinity",
            members=frozenset({"a", "b"}),
        )
        # Only 'a' is visible; 'b' is outside the aggregate - not a violation.
        violations = _detect_violations(
            [group], {"h1": [_vm("a", "h1")]},
        )
        assert violations == []

    def test_anti_affinity_violation(self) -> None:
        group = ServerGroup(
            group_id="g",
            policy="anti-affinity",
            members=frozenset({"a", "b"}),
        )
        # Both on h1.
        violations = _detect_violations(
            [group], {"h1": [_vm("a", "h1"), _vm("b", "h1")]},
        )
        assert len(violations) == 1
        assert violations[0].offending_uuids == frozenset({"a", "b"})

    def test_anti_affinity_respected_across_hosts(self) -> None:
        group = ServerGroup(
            group_id="g",
            policy="anti-affinity",
            members=frozenset({"a", "b"}),
        )
        # Spread across h1 and h2 - compliant.
        violations = _detect_violations(
            [group],
            {"h1": [_vm("a", "h1")], "h2": [_vm("b", "h2")]},
        )
        assert violations == []

    def test_max_server_per_host_allows_members_within_cap(self) -> None:
        """With cap=2, two members sharing a host is compliant, not a violation."""
        group = ServerGroup(
            group_id="g",
            policy="anti-affinity",
            members=frozenset({"a", "b", "c"}),
            max_server_per_host=2,
        )
        # Two on h1 (within cap), one on h2 - no host exceeds the cap.
        violations = _detect_violations(
            [group],
            {"h1": [_vm("a", "h1"), _vm("b", "h1")], "h2": [_vm("c", "h2")]},
        )
        assert violations == []

    def test_max_server_per_host_violation_when_over_cap(self) -> None:
        """With cap=2, a host holding three members is a violation."""
        group = ServerGroup(
            group_id="g",
            policy="anti-affinity",
            members=frozenset({"a", "b", "c"}),
            max_server_per_host=2,
        )
        violations = _detect_violations(
            [group],
            {"h1": [_vm("a", "h1"), _vm("b", "h1"), _vm("c", "h1")]},
        )
        assert len(violations) == 1
        # All three co-located members are flagged; the scoring pass
        # moves them one at a time until h1 is back within the cap.
        assert violations[0].offending_uuids == frozenset({"a", "b", "c"})

    def test_affinity_violation_when_members_spread(self) -> None:
        group = ServerGroup(
            group_id="g",
            policy="affinity",
            members=frozenset({"a", "b"}),
        )
        violations = _detect_violations(
            [group],
            {"h1": [_vm("a", "h1")], "h2": [_vm("b", "h2")]},
        )
        assert len(violations) == 1
        assert violations[0].offending_uuids == frozenset({"a", "b"})

    def test_affinity_satisfied_on_same_host(self) -> None:
        group = ServerGroup(
            group_id="g",
            policy="affinity",
            members=frozenset({"a", "b"}),
        )
        violations = _detect_violations(
            [group],
            {"h1": [_vm("a", "h1"), _vm("b", "h1")]},
        )
        assert violations == []


class TestAntiAffinityOffenders:
    def test_pair_on_same_host(self) -> None:
        placement = {"a": "h1", "b": "h1"}
        assert _anti_affinity_offenders({"a", "b"}, placement) == {"a", "b"}

    def test_triple_all_colocated(self) -> None:
        placement = {"a": "h1", "b": "h1", "c": "h1"}
        assert _anti_affinity_offenders({"a", "b", "c"}, placement) == {
            "a", "b", "c",
        }

    def test_one_isolated_not_an_offender(self) -> None:
        placement = {"a": "h1", "b": "h1", "c": "h2"}
        assert _anti_affinity_offenders({"a", "b", "c"}, placement) == {"a", "b"}


class TestAffinityEnforcerDisabled:
    def test_both_flags_false_no_ops(self, constraints: MagicMock) -> None:
        enforcer = AffinityEnforcer(
            constraints, enforce_hard=False, enforce_soft=False,
        )
        assert enforcer.enabled is False

        plan, _scores, _vms_by_host, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result("mem", [HostScore("h1", 0.5, 0.5), HostScore("h2", 0.1, 0.1)]),
            ],
            vm_profiles={"a": _vm("a", "h1"), "b": _vm("b", "h1")},
            budget=3,
        )
        assert plan.steps == []
        assert remaining == 3
        constraints.get_groups.assert_not_called()

    def test_budget_zero_no_ops(self, constraints: MagicMock) -> None:
        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result("mem", [HostScore("h1", 0.5, 0.5)]),
            ],
            vm_profiles={"a": _vm("a", "h1")},
            budget=0,
        )
        assert plan.steps == []
        assert remaining == 0


class TestAffinityEnforcerHardRepair:
    def test_anti_affinity_violation_emits_move(self, constraints: MagicMock) -> None:
        """Two anti-affinity members share a host → one must move."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _scores, _vms_by_host, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.6, 0.6), HostScore("h2", 0.1, 0.1)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
            },
            budget=3,
        )
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.phase == MigrationPhase.AFFINITY
        assert step.from_host == "h1"
        assert step.to_host == "h2"
        assert remaining == 2

    def test_affinity_violation_consolidates(self, constraints: MagicMock) -> None:
        """Two affinity members on different hosts → move one to join the other."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, _ = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.1, 0.1), HostScore("h2", 0.1, 0.1)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h2"),
            },
            budget=3,
        )
        assert len(plan.steps) == 1
        assert plan.steps[0].phase == MigrationPhase.AFFINITY
        assert plan.steps[0].from_host in {"h1", "h2"}
        assert plan.steps[0].to_host in {"h1", "h2"}
        assert plan.steps[0].from_host != plan.steps[0].to_host

    def test_soft_ignored_when_only_hard_enforced(self, constraints: MagicMock) -> None:
        """A soft-anti-affinity violation is not repaired when only hard is on."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="soft-anti-affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.1, 0.1), HostScore("h2", 0.1, 0.1)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
            },
            budget=3,
        )
        assert plan.steps == []
        assert remaining == 3

    def test_hard_ignored_when_only_soft_enforced(self, constraints: MagicMock) -> None:
        """A hard anti-affinity violation is not repaired when only soft is on."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=False, enforce_soft=True,
        )
        plan, _, _, _ = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.1, 0.1), HostScore("h2", 0.1, 0.1)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
            },
            budget=3,
        )
        assert plan.steps == []


class TestAffinityEnforcerRespectThreshold:
    def test_skips_move_that_would_cross_threshold(
        self, constraints: MagicMock,
    ) -> None:
        """An enforcement move that would push a policy above threshold is rejected.

        Policy threshold is 0.15.  h1 score 0.3, h2 score 0.3 (balanced).
        A VM with weight 0.5 on h1 is in anti-affinity with a sibling on
        h1 - but moving it to h2 would make scores h1=-0.2, h2=0.8 ->
        imbalance 1.0, well above 0.15.  Enforcement must back off.
        """
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy(threshold=0.15)],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.3, 0.3), HostScore("h2", 0.3, 0.3)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1", weights={"mem": 0.5}),
                "b": _vm("b", "h1", weights={"mem": 0.01}),
            },
            budget=3,
        )
        # Only the lightweight 'b' is movable without crossing threshold.
        assert len(plan.steps) == 1
        assert plan.steps[0].instance_uuid == "b"
        assert remaining == 2

    def test_no_legal_destination_stops_gracefully(
        self, constraints: MagicMock,
    ) -> None:
        """When neither candidate move is legal, the enforcer gives up."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b"}),
            ),
        ]
        # ConstraintChecker refuses every move (e.g. another group blocks).
        constraints.check.return_value = False

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [HostScore("h1", 0.1, 0.1), HostScore("h2", 0.1, 0.1)],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
            },
            budget=3,
        )
        assert plan.steps == []
        assert remaining == 3


class TestAffinityEnforcerCascading:
    def test_three_member_anti_affinity_on_one_host(
        self, constraints: MagicMock,
    ) -> None:
        """3 anti-affinity members on h1 → 2 repair moves needed.

        With 3 hosts and enough budget, the enforcer iterates and clears
        the violation fully.
        """
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b", "c"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, _ = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [
                        HostScore("h1", 0.3, 0.3),
                        HostScore("h2", 0.0, 0.0),
                        HostScore("h3", 0.0, 0.0),
                    ],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
                "c": _vm("c", "h1"),
            },
            budget=3,
        )
        # 3 members on h1 → must move 2 away.
        assert len(plan.steps) == 2
        assert all(step.from_host == "h1" for step in plan.steps)
        assert all(step.phase == MigrationPhase.AFFINITY for step in plan.steps)

    def test_budget_limits_cascade(self, constraints: MagicMock) -> None:
        """Budget of 1 caps the repair at a single move even if more are possible."""
        constraints.get_groups.return_value = [
            ServerGroup(
                group_id="g",
                policy="anti-affinity",
                members=frozenset({"a", "b", "c"}),
            ),
        ]
        constraints.check.return_value = True

        enforcer = AffinityEnforcer(
            constraints, enforce_hard=True, enforce_soft=False,
        )
        plan, _, _, remaining = enforcer.enforce(
            aggregate="agg",
            policies=[_policy()],
            policy_results=[
                _policy_result(
                    "mem",
                    [
                        HostScore("h1", 0.3, 0.3),
                        HostScore("h2", 0.0, 0.0),
                        HostScore("h3", 0.0, 0.0),
                    ],
                ),
            ],
            vm_profiles={
                "a": _vm("a", "h1"),
                "b": _vm("b", "h1"),
                "c": _vm("c", "h1"),
            },
            budget=1,
        )
        assert len(plan.steps) == 1
        assert remaining == 0

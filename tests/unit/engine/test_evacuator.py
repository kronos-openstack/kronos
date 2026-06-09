"""Tests for the disabled-host evacuator."""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import MagicMock

import pytest

from kronos.clients.nova import ComputeService
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.evacuator import Evacuator
from kronos.engine.types import MigrationPhase, VmProfile
from kronos.policies.models import PolicyConfig, PolicyMode


def _policy(
    name: str = "p1",
    weight: float = 1.0,
    threshold: float = 0.2,
) -> PolicyConfig:
    return PolicyConfig(
        name=name,
        mode=PolicyMode.SPREAD,
        weight=weight,
        imbalance_query="ignored",
        threshold=threshold,
    )


def _vm(uuid: str, host: str, weight: float = 0.1) -> VmProfile:
    return VmProfile(
        instance_uuid=uuid,
        instance_name=f"vm-{uuid}",
        host=host,
        weights={"p1": weight},
        sources={"p1": "prometheus"},
    )


def _services(
    *,
    up: Sequence[str],
    disabled: Sequence[str] = (),
    down: Sequence[str] = (),
) -> dict[str, ComputeService]:
    out: dict[str, ComputeService] = {}
    for h in up:
        out[h] = ComputeService(
            host=h, binary="nova-compute", state="up", status="enabled",
        )
    for h in disabled:
        out[h] = ComputeService(
            host=h, binary="nova-compute", state="up", status="disabled",
        )
    for h in down:
        out[h] = ComputeService(
            host=h, binary="nova-compute", state="down", status="enabled",
        )
    return out


@pytest.fixture()
def constraints():
    """Permissive constraint checker (no server groups)."""
    nova = MagicMock()
    nova.list_server_groups.return_value = []
    c = ConstraintChecker(nova)
    return c


def _setup(constraints: ConstraintChecker, services: dict[str, ComputeService]):
    constraints.set_services(services)


class TestEvacuatorDisabled:
    def test_returns_passthrough_when_disabled(self, constraints) -> None:
        services = _services(up=["h1"], disabled=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=False)
        scores = {"p1": {"h1": 0.1, "h2": 0.5}}
        vms_by_host = {"h2": [_vm("v1", "h2")]}
        vm_profiles = {"v1": _vm("v1", "h2")}

        plan, out_scores, out_vms, remaining = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy()],
            scores=scores,
            vms_by_host=vms_by_host,
            vm_profiles=vm_profiles,
            services=services,
            budget=5,
        )

        assert plan.steps == []
        assert remaining == 5
        # Untouched
        assert out_scores is scores
        assert out_vms is vms_by_host

    def test_returns_passthrough_when_no_disabled_hosts(self, constraints) -> None:
        services = _services(up=["h1", "h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        plan, _, _, remaining = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy()],
            scores={"p1": {"h1": 0.5, "h2": 0.5}},
            vms_by_host={"h1": [_vm("v1", "h1")]},
            vm_profiles={"v1": _vm("v1", "h1")},
            services=services,
            budget=3,
        )
        assert plan.steps == []
        assert remaining == 3


class TestEvacuatorBasic:
    def test_evacuates_disabled_host(self, constraints) -> None:
        services = _services(up=["h1"], disabled=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        plan, _, _, remaining = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy(threshold=1.0)],
            scores={"p1": {"h1": 0.1, "h2": 0.4}},
            vms_by_host={"h2": [_vm("v1", "h2")]},
            vm_profiles={"v1": _vm("v1", "h2")},
            services=services,
            budget=3,
        )

        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.instance_uuid == "v1"
        assert step.from_host == "h2"
        assert step.to_host == "h1"
        assert step.phase == MigrationPhase.EVACUATE
        assert remaining == 2

    def test_aggregate_scoping_ignores_other_aggregates(
        self, constraints,
    ) -> None:
        """Cluster-wide service map; only this aggregate's hosts count."""
        services = _services(
            up=["h1"],
            disabled=["h2", "h-other-agg"],
        )
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        # Aggregate scope excludes h-other-agg.
        plan, _, _, _ = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy(threshold=1.0)],
            scores={"p1": {"h1": 0.1, "h2": 0.4}},
            vms_by_host={"h2": [_vm("v1", "h2")]},
            vm_profiles={"v1": _vm("v1", "h2")},
            services=services,
            budget=3,
        )

        # Only v1 (in this aggregate) is evacuated; nothing leaks from
        # h-other-agg even though that host is also disabled.
        assert len(plan.steps) == 1
        assert plan.steps[0].instance_uuid == "v1"

    def test_skips_state_down_hosts(self, constraints) -> None:
        """state=down hosts cannot be live-migrated off; skipped."""
        services = _services(up=["h1"], down=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        plan, _, _, _ = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy(threshold=1.0)],
            scores={"p1": {"h1": 0.1, "h2": 0.4}},
            vms_by_host={"h2": [_vm("v1", "h2")]},
            vm_profiles={"v1": _vm("v1", "h2")},
            services=services,
            budget=3,
        )
        assert plan.steps == []

    def test_respects_budget(self, constraints) -> None:
        services = _services(up=["h1"], disabled=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        # Three VMs to evacuate but budget = 1.
        plan, _, _, remaining = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy(threshold=1.0)],
            scores={"p1": {"h1": 0.0, "h2": 0.6}},
            vms_by_host={
                "h2": [_vm("v1", "h2"), _vm("v2", "h2"), _vm("v3", "h2")],
            },
            vm_profiles={
                "v1": _vm("v1", "h2"),
                "v2": _vm("v2", "h2"),
                "v3": _vm("v3", "h2"),
            },
            services=services,
            budget=1,
        )
        assert len(plan.steps) == 1
        assert remaining == 0

    def test_zero_budget_short_circuits(self, constraints) -> None:
        services = _services(up=["h1"], disabled=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        plan, _, _, remaining = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy()],
            scores={"p1": {"h1": 0.1, "h2": 0.4}},
            vms_by_host={"h2": [_vm("v1", "h2")]},
            vm_profiles={"v1": _vm("v1", "h2")},
            services=services,
            budget=0,
        )
        assert plan.steps == []
        assert remaining == 0


class TestEvacuatorThreshold:
    def test_refuses_move_that_breaches_threshold(self, constraints) -> None:
        """Evacuation must respect policy thresholds like the planner."""
        services = _services(up=["h1"], disabled=["h2"])
        _setup(constraints, services)

        ev = Evacuator(constraints, enabled=True)
        # Tiny threshold; moving the VM to h1 worsens p1 imbalance.
        # Starting imbalance = 0.5 - 0.5 = 0; after move, h1=0.7, h2=0.3.
        plan, _, _, _ = ev.evacuate(
            aggregate="agg",
            aggregate_hosts=["h1", "h2"],
            policies=[_policy(threshold=0.05)],
            scores={"p1": {"h1": 0.5, "h2": 0.5}},
            vms_by_host={"h2": [_vm("v1", "h2", weight=0.2)]},
            vm_profiles={"v1": _vm("v1", "h2", weight=0.2)},
            services=services,
            budget=3,
        )
        # No legal destination given threshold; nothing emitted.
        assert plan.steps == []

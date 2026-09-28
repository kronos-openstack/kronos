from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from oslo_config import cfg

from kronos.clients.nova import ComputeService, Instance, NovaClient
from kronos.clients.placement import PlacementClient
from kronos.clients.prometheus import PrometheusClient, PrometheusHealth, QueryResult
from kronos.cmd.replay import ReplayNovaClient, ReplayPlacementClient, ReplayPrometheusClient
from kronos.common.config import register_opts
from kronos.common.exceptions import PlacementClientError
from kronos.common.snapshot import write_snapshot
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.loop import EngineLoop
from kronos.engine.placement import PlacementGate
from kronos.engine.types import CycleReport, MigrationPlan, VmProfile
from kronos.policies.models import PoliciesConfig, PolicyConfig, PolicyMode, VmProfileLabelType

AVX = "HW_CPU_X86_AVX"
AVX512 = "HW_CPU_X86_AVX512F"


def _services() -> dict[str, ComputeService]:
    return {
        host: ComputeService(
            host=host, binary="nova-compute", state="up",
            status="enabled", zone="nova",
        )
        for host in ("h1", "h2")
    }


def _checker(enabled: bool = True) -> ConstraintChecker:
    nova = MagicMock()
    nova.list_server_groups.return_value = []
    checker = ConstraintChecker(nova, require_cpu_compatibility=enabled)
    checker.set_services(_services())
    return checker


def _vm() -> VmProfile:
    return VmProfile(instance_uuid="v1", instance_name="vm", host="h1")


@pytest.mark.parametrize(("source", "destination", "expected"), [
    ({AVX}, {AVX, AVX512}, True),
    ({AVX}, {AVX}, True),
    ({AVX, AVX512}, {AVX}, False),
    ({"HW_CPU_X86_AMD_SVM"}, {"HW_CPU_X86_INTEL_VMX"}, False),
    (set(), {AVX}, True),
    (set(), set(), True),
    ({AVX}, set(), False),
    (None, {AVX}, False),
    ({AVX}, None, False),
    (set(), None, False),
    (None, None, False),
])
def test_cpu_truth_table(source, destination, expected) -> None:
    checker = _checker()
    checker.set_cpu_traits({
        host: frozenset(flags)
        for host, flags in (("h1", source), ("h2", destination))
        if flags is not None
    })
    assert checker.check(_vm(), "h2", {}) is expected


def test_disabled_cpu_gate_accepts_missing_traits() -> None:
    assert _checker(enabled=False).check(_vm(), "h2", {})


def test_cpu_gate_fails_closed_before_install_and_after_invalidation() -> None:
    checker = _checker()
    assert not checker.check(_vm(), "h2", {})
    checker.set_cpu_traits({"h1": frozenset({AVX}), "h2": frozenset({AVX})})
    assert checker.check(_vm(), "h2", {})
    checker.invalidate_cache()
    checker.set_services(_services())
    assert not checker.check(_vm(), "h2", {})
    checker.set_cpu_traits({"h1": frozenset({AVX}), "h2": frozenset({AVX})})
    assert checker.check(_vm(), "h2", {})


def test_rejections_are_memoized_and_refresh_with_traits(caplog) -> None:
    checker = _checker()
    traits = {"h1": frozenset({AVX512}), "h2": frozenset({AVX})}
    checker.set_cpu_traits(traits)
    with caplog.at_level("DEBUG"):
        assert not checker.check(_vm(), "h2", {})
        assert not checker.check(_vm(), "h2", {})
    assert sum(AVX512 in r.message for r in caplog.records) == 1
    traits["h2"] = traits["h1"]
    checker.set_cpu_traits(traits)
    assert checker.check(_vm(), "h2", {})


def test_empty_host_warns_once_at_install(caplog) -> None:
    checker = _checker()
    with caplog.at_level("WARNING"):
        checker.set_cpu_traits({"h1": frozenset(), "h2": frozenset({AVX})})
        assert checker.check(_vm(), "h2", {})
        assert checker.check(_vm(), "h2", {})
    assert sum("blind" in r.message for r in caplog.records) == 1


def test_gate_order() -> None:
    checker = _checker()
    gate = MagicMock(spec=PlacementGate)
    gate.is_destination_ok.return_value = False
    checker.set_placement_gate(gate)
    checker.set_services({})
    assert not checker.check(_vm(), "h2", {})
    gate.is_destination_ok.assert_not_called()
    checker.set_services(_services())
    assert not checker.check(_vm(), "h2", {})
    assert not checker._cpu_compatibility
    gate.is_destination_ok.return_value = True
    with patch.object(checker, "_get_groups") as groups:
        assert not checker.check(_vm(), "h2", {})
        groups.assert_not_called()
        checker.set_cpu_traits({"h1": frozenset({AVX}), "h2": frozenset({AVX})})
        groups.return_value = []
        assert checker.check(_vm(), "h2", {})
        groups.assert_called_once()


def _scenario(phase: str):
    conf = cfg.ConfigOpts()
    register_opts(conf)
    for name, value in {
        "require_cpu_compatibility": True,
        "enforce_placement_claims": False,
        "cooldown": 0,
        "instance_cooldown": 0,
        "evacuate_disabled_hosts": phase == "evacuate",
        "enforce_hard_affinity": phase == "affinity",
    }.items():
        conf.set_override(name, value, group="engine")
    nova = MagicMock(spec=NovaClient)
    services = _services()
    if phase == "evacuate":
        services["h1"].status = "disabled"
    nova.list_compute_services.return_value = list(services.values())
    nova.get_hosts_in_aggregate.return_value = ["h1", "h2"]
    nova.list_compute_hosts.return_value = []
    nova.list_server_groups.return_value = (
        [{"id": "g1", "policies": ["anti-affinity"], "members": ["v1", "v2"]}]
        if phase == "affinity" else []
    )
    instances = [
        Instance(
            uuid=vm, name=vm, internal_name=vm, host="h1",
            flavor_vcpus=1, flavor_ram_mb=1024, status="ACTIVE",
        )
        for vm in (["v1", "v2"] if phase == "affinity" else ["v1"])
    ]
    nova.list_instances_on_host.side_effect = lambda host: (
        instances if host == "h1" else []
    )
    policy = PolicyConfig(
        name="cpu", mode=PolicyMode.PACK if phase == "pack" else PolicyMode.SPREAD,
        weight=1.0, imbalance_query="load", vm_profile_query="vms",
        vm_profile_label_type=VmProfileLabelType.NOVA_INSTANCE_UUID,
        threshold=0.15, capacity_query="capacity" if phase == "pack" else None,
        capacity_threshold=0.9, max_migrations_per_cycle=1,
    )
    prometheus = MagicMock(spec=PrometheusClient)
    series = {
        "load": {"h1": 0.2, "h2": 0.6} if phase == "pack" else {"h1": 0.8, "h2": 0.2},
        "vms": {"v1": 0.2 if phase == "pack" else 0.3, "v2": 0.1},
    }
    prometheus.instant_query.side_effect = lambda query, **kwargs: QueryResult(
        query=query, timestamp=datetime.now(tz=UTC), health=PrometheusHealth.HEALTHY,
        series=series[query],
    )
    placement = MagicMock(spec=PlacementClient)
    placement.fetch_cpu_traits.return_value = {
        "h1": frozenset({AVX, AVX512}), "h2": frozenset({AVX}),
    }
    return conf, nova, prometheus, placement, PoliciesConfig(policies=[policy])


@pytest.mark.parametrize("phase", ["spread", "pack", "evacuate", "affinity"])
def test_all_movers_reject_incompatible_destination_and_recover(phase) -> None:
    conf, nova, prometheus, placement, policies = _scenario(phase)
    engine = EngineLoop(conf, nova=nova, prometheus=prometheus, placement=placement)
    blocked = engine.run_once(policies=policies, aggregates=["a"])
    assert not blocked.errors
    assert _plan(blocked).migration_count == 0
    placement.fetch_cpu_traits.assert_called_once_with({"h1", "h2"})
    placement.fetch_cpu_traits.return_value["h2"] = frozenset({AVX, AVX512})
    allowed = engine.run_once(policies=policies, aggregates=["a"])
    steps = _plan(allowed).steps
    assert len(steps) == 1
    assert (steps[0].from_host, steps[0].to_host, steps[0].phase.value) == ("h1", "h2", phase)


def test_traits_failure_discards_previous_cycle_data() -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    placement.fetch_cpu_traits.return_value["h2"] = frozenset({AVX, AVX512})
    engine = EngineLoop(conf, nova=nova, prometheus=prometheus, placement=placement)
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 1
    placement.fetch_cpu_traits.side_effect = PlacementClientError(reason="unavailable")
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 0


def test_disabled_option_skips_placement_entirely() -> None:
    conf, nova, prometheus, _placement, policies = _scenario("spread")
    conf.set_override("require_cpu_compatibility", False, group="engine")
    with patch("kronos.engine.loop.PlacementClient") as constructor:
        engine = EngineLoop(conf, nova=nova, prometheus=prometheus)
    constructor.assert_not_called()
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 1


def test_record_replay_cpu_traits_round_trip(tmp_path: Path) -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    target = write_snapshot(
        tmp_path, nova, prometheus, policies, ["a"], placement=placement,
    )
    data = json.loads((target / "placement" / "cpu_traits.json").read_text())
    assert data == {"h1": sorted([AVX, AVX512]), "h2": [AVX]}
    engine = EngineLoop(
        conf,
        nova=cast(NovaClient, ReplayNovaClient(target)),
        prometheus=cast(PrometheusClient, ReplayPrometheusClient(target)),
        placement=ReplayPlacementClient(target),
    )
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 0
    (target / "placement" / "cpu_traits.json").write_text(
        json.dumps({"h1": [AVX, AVX512], "h2": [AVX, AVX512]}),
    )
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 1


@pytest.mark.parametrize("flags", [None, {}, [], {"h1": None}, {"h1": [1]}])
def test_old_or_invalid_snapshot_does_not_allow_moves(tmp_path: Path, flags) -> None:
    if flags is not None:
        (tmp_path / "placement").mkdir()
        (tmp_path / "placement" / "cpu_traits.json").write_text(json.dumps(flags))
    conf, nova, prometheus, _placement, policies = _scenario("spread")
    engine = EngineLoop(
        conf, nova=nova, prometheus=prometheus, placement=ReplayPlacementClient(tmp_path),
    )
    assert _plan(engine.run_once(
        policies=policies, aggregates=["a"],
    )).migration_count == 0


def _plan(report: CycleReport) -> MigrationPlan:
    assert not report.errors
    plan = report.aggregate_results[0].migration_plan
    assert plan is not None
    return plan


@pytest.mark.parametrize("payload", [
    {"traits": [AVX, AVX512, "CUSTOM_CPU", "COMPUTE_STATUS_DISABLED"]},
    {"traits": []},
])
def test_client_filters_cpu_traits_and_scope(payload) -> None:
    client = object.__new__(PlacementClient)
    client._conn = MagicMock()
    proxy = client._conn.placement
    providers = []
    for host in ("h1", "outside", "sharing-pool"):
        provider = MagicMock()
        provider.name = host
        provider.id = f"rp-{host}"
        providers.append(provider)
    proxy.resource_providers.return_value = providers
    proxy.get.return_value.json.return_value = payload
    assert client.fetch_cpu_traits({"h1", "missing"}) == {
        "h1": frozenset(t for t in payload["traits"] if t.startswith("HW_CPU_")),
    }
    proxy.get.assert_called_once_with("/resource_providers/rp-h1/traits", microversion="1.6")
    proxy.get.return_value.raise_for_status.assert_called_once()


@pytest.mark.parametrize("payload", [{}, {"traits": None}, {"traits": AVX}, {"traits": [1]}])
def test_client_rejects_malformed_traits(payload) -> None:
    client = object.__new__(PlacementClient)
    client._conn = MagicMock()
    provider = MagicMock()
    provider.name = "h1"
    client._conn.placement.resource_providers.return_value = [provider]
    client._conn.placement.get.return_value.json.return_value = payload
    with pytest.raises(PlacementClientError):
        client.fetch_cpu_traits({"h1"})


@pytest.mark.parametrize("failure", ["list", "get", "http"])
def test_client_wraps_traits_fetch_failures(failure) -> None:
    client = object.__new__(PlacementClient)
    client._conn = MagicMock()
    proxy = client._conn.placement
    provider = MagicMock()
    provider.name = "h1"
    proxy.resource_providers.return_value = [provider]
    operation = {
        "list": proxy.resource_providers,
        "get": proxy.get,
        "http": proxy.get.return_value.raise_for_status,
    }[failure]
    operation.side_effect = RuntimeError("Placement unavailable")
    with pytest.raises(PlacementClientError):
        client.fetch_cpu_traits({"h1"})


def test_no_scope_performs_no_placement_requests() -> None:
    client = object.__new__(PlacementClient)
    client._conn = MagicMock()
    assert client.fetch_cpu_traits(set()) == {}
    assert not client._conn.mock_calls


def test_cpu_scope_is_fetched_once_for_overlapping_aggregates_and_az() -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    outside = ComputeService(
        host="h3", binary="nova-compute", state="up", status="enabled", zone="other",
    )
    nova.list_compute_services.return_value.append(outside)
    scopes = {"a": ["h1", "h2", "h3"], "b": ["h2"], None: ["h3"]}
    nova.get_hosts_in_aggregate.side_effect = scopes.__getitem__
    engine = EngineLoop(conf, nova=nova, prometheus=prometheus, placement=placement)
    report = engine.run_once(policies=policies, aggregates=["a", "b", None, "missing"])
    assert len(report.errors) == 1
    assert "missing" in report.errors[0]
    placement.fetch_cpu_traits.assert_called_once_with({"h1", "h2"})
    assert nova.get_hosts_in_aggregate.call_count == 4


def test_cpu_and_claims_gates_are_independent() -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    conf.set_override("enforce_placement_claims", True, group="engine")
    placement.fetch_snapshots.return_value = {}
    placement.fetch_cpu_traits.return_value["h2"] = frozenset({AVX, AVX512})
    engine = EngineLoop(conf, nova=nova, prometheus=prometheus, placement=placement)
    assert _plan(engine.run_once(policies=policies, aggregates=["a"])).migration_count == 0
    placement.fetch_snapshots.assert_called_once()
    placement.fetch_cpu_traits.assert_called_once()


def test_cpu_only_engine_constructs_placement_client() -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    with patch("kronos.engine.loop.PlacementClient", return_value=placement) as constructor:
        engine = EngineLoop(conf, nova=nova, prometheus=prometheus)
    constructor.assert_called_once_with(conf)
    assert _plan(engine.run_once(policies=policies, aggregates=["a"])).migration_count == 0


def test_engine_snapshot_contains_cpu_traits(tmp_path: Path) -> None:
    conf, nova, prometheus, placement, policies = _scenario("spread")
    conf.set_override("snapshot_dir", str(tmp_path), group="engine")
    engine = EngineLoop(conf, nova=nova, prometheus=prometheus, placement=placement)
    engine._write_engine_snapshot(policies, ["a"])
    path, = tmp_path.glob("*/placement/cpu_traits.json")
    assert json.loads(path.read_text())["h1"] == sorted([AVX, AVX512])


def test_failed_traits_snapshot_is_missing_data_not_empty_features(tmp_path: Path) -> None:
    _conf, nova, prometheus, placement, policies = _scenario("spread")
    placement.fetch_cpu_traits.side_effect = PlacementClientError(reason="unavailable")
    target = write_snapshot(tmp_path, nova, prometheus, policies, ["a"], placement=placement)
    assert ReplayPlacementClient(target).fetch_cpu_traits({"h1", "h2"}) == {}
    assert (target / "nova" / "instances.json").exists()

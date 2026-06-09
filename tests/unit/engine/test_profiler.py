"""Tests for the VM profiler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from kronos.clients.nova import Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.engine.profiler import VmProfiler, _apply_fallback, _instance_prom_key
from kronos.policies.models import (
    PolicyConfig,
    VmProfileFallback,
    VmProfileLabelType,
)


def _make_policy(**overrides: Any) -> PolicyConfig:
    defaults: dict[str, Any] = {
        "name": "test-policy",
        "mode": "spread",
        "weight": 1.0,
        "imbalance_query": "test_metric",
        "vm_profile_query": "vm_metric",
        "vm_profile_label": "instance_name",
        "vm_profile_label_type": "nova_internal_name",
        "vm_profile_fallback": "skip",
        "threshold": 0.15,
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_instance(
    uuid: str = "uuid-1",
    name: str = "vm-1",
    internal_name: str = "instance-00001",
    host: str = "h1",
    vcpus: int = 4,
    status: str = "ACTIVE",
) -> Instance:
    return Instance(
        uuid=uuid,
        name=name,
        internal_name=internal_name,
        host=host,
        flavor_vcpus=vcpus,
        flavor_ram_mb=8192,
        status=status,
    )


def _make_query_result(series: dict[str, float]) -> QueryResult:
    return QueryResult(
        query="vm_metric",
        timestamp=datetime.now(tz=UTC),
        health=PrometheusHealth.HEALTHY,
        series=series,
        missing_labels=set(),
        warnings=[],
    )


@pytest.fixture()
def mock_prometheus() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def mock_nova() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def profiler(mock_prometheus: MagicMock, mock_nova: MagicMock) -> VmProfiler:
    return VmProfiler(mock_prometheus, mock_nova)


class TestInstancePromKey:
    def test_internal_name(self) -> None:
        inst = _make_instance(internal_name="instance-00042")
        assert _instance_prom_key(inst, VmProfileLabelType.NOVA_INTERNAL_NAME) == "instance-00042"

    def test_uuid(self) -> None:
        inst = _make_instance(uuid="abc-123")
        assert _instance_prom_key(inst, VmProfileLabelType.NOVA_INSTANCE_UUID) == "abc-123"

    def test_display_name(self) -> None:
        inst = _make_instance(name="my-server")
        assert _instance_prom_key(inst, VmProfileLabelType.NOVA_DISPLAY_NAME) == "my-server"


class TestApplyFallback:
    def test_skip_returns_none(self) -> None:
        inst = _make_instance()
        from collections import Counter
        result = _apply_fallback(
            inst, VmProfileFallback.SKIP, {"h1": 0.5}, Counter({"h1": 2}),
        )
        assert result is None

    def test_host_average(self) -> None:
        inst = _make_instance(host="h1")
        from collections import Counter
        result = _apply_fallback(
            inst, VmProfileFallback.HOST_AVERAGE, {"h1": 0.8}, Counter({"h1": 4}),
        )
        assert result == pytest.approx(0.2)

    def test_host_average_single_vm(self) -> None:
        inst = _make_instance(host="h1")
        from collections import Counter
        result = _apply_fallback(
            inst, VmProfileFallback.HOST_AVERAGE, {"h1": 0.6}, Counter({"h1": 1}),
        )
        assert result == pytest.approx(0.6)


class TestVmProfilerCollect:
    def test_basic_collection_single_policy(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1"),
            _make_instance(uuid="u2", internal_name="inst-002", host="h2"),
        ]
        mock_nova.list_instances_on_host.side_effect = [[instances[0]], [instances[1]]]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"inst-001": 0.3, "inst-002": 0.7},
        )

        policy = _make_policy()
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1", "h2"],
            host_scores_by_policy={policy.name: {"h1": 0.4, "h2": 0.8}},
        )

        assert len(profiles) == 2
        assert profiles["u1"].weights["test-policy"] == pytest.approx(0.3)
        assert profiles["u2"].weights["test-policy"] == pytest.approx(0.7)
        assert profiles["u1"].sources["test-policy"] == "prometheus"

    def test_multi_policy_collection(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1"),
        ]
        mock_nova.list_instances_on_host.return_value = instances

        def query_side_effect(query, label_key, expected_labels=None):
            if query == "cpu_metric":
                return _make_query_result({"inst-001": 0.1})
            if query == "mem_metric":
                return _make_query_result({"inst-001": 0.4})
            return _make_query_result({})

        mock_prometheus.instant_query.side_effect = query_side_effect

        cpu = _make_policy(
            name="cpu", weight=0.5, imbalance_query="cpu_host",
            vm_profile_query="cpu_metric",
        )
        mem = _make_policy(
            name="mem", weight=0.5, imbalance_query="mem_host",
            vm_profile_query="mem_metric",
        )
        profiles = profiler.collect(
            policies=[cpu, mem],
            hosts=["h1"],
            host_scores_by_policy={
                "cpu": {"h1": 0.1},
                "mem": {"h1": 0.4},
            },
        )

        assert len(profiles) == 1
        assert profiles["u1"].weights["cpu"] == pytest.approx(0.1)
        assert profiles["u1"].weights["mem"] == pytest.approx(0.4)

    def test_no_instances(
        self, profiler: VmProfiler, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_instances_on_host.return_value = []
        policy = _make_policy()
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1"],
            host_scores_by_policy={policy.name: {"h1": 0.5}},
        )
        assert profiles == {}

    def test_skip_fallback_excludes_vm(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1"),
            _make_instance(uuid="u2", internal_name="inst-002", host="h1"),
        ]
        mock_nova.list_instances_on_host.return_value = instances
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"inst-001": 0.5},
        )

        policy = _make_policy(vm_profile_fallback="skip")
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1"],
            host_scores_by_policy={policy.name: {"h1": 0.6}},
        )

        assert len(profiles) == 1
        assert "u1" in profiles
        assert "u2" not in profiles

    def test_host_average_fallback(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1"),
            _make_instance(uuid="u2", internal_name="inst-002", host="h1"),
        ]
        mock_nova.list_instances_on_host.return_value = instances
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"inst-001": 0.5},
        )

        policy = _make_policy(vm_profile_fallback="host_average")
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1"],
            host_scores_by_policy={policy.name: {"h1": 0.8}},
        )

        assert len(profiles) == 2
        assert profiles["u1"].sources["test-policy"] == "prometheus"
        assert profiles["u2"].sources["test-policy"] == "fallback:host_average"
        assert profiles["u2"].weights["test-policy"] == pytest.approx(0.4)

    def test_skips_non_active_instances(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1", status="ACTIVE"),
            _make_instance(uuid="u2", internal_name="inst-002", host="h1", status="SHUTOFF"),
            _make_instance(uuid="u3", internal_name="inst-003", host="h1", status="ERROR"),
            _make_instance(uuid="u4", internal_name="inst-004", host="h1", status="MIGRATING"),
        ]
        mock_nova.list_instances_on_host.return_value = instances
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"inst-001": 0.5, "inst-002": 0.5, "inst-003": 0.5, "inst-004": 0.5},
        )

        policy = _make_policy()
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1"],
            host_scores_by_policy={policy.name: {"h1": 0.5}},
        )

        assert set(profiles.keys()) == {"u1"}

    def test_no_vm_profile_query(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [_make_instance(uuid="u1", internal_name="inst-001", host="h1")]
        mock_nova.list_instances_on_host.return_value = instances

        policy = _make_policy(vm_profile_query=None, vm_profile_fallback="host_average")
        profiles = profiler.collect(
            policies=[policy],
            hosts=["h1"],
            host_scores_by_policy={policy.name: {"h1": 0.6}},
        )

        assert len(profiles) == 1
        assert profiles["u1"].sources["test-policy"] == "fallback:host_average"
        mock_prometheus.instant_query.assert_not_called()

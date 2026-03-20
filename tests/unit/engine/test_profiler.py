"""Tests for the VM profiler."""

from __future__ import annotations

from datetime import UTC, datetime
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


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "test-policy",
        "mode": "spread",
        "aggregate": "test-agg",
        "imbalance_query": "test_metric",
        "vm_profile_query": "vm_metric",
        "vm_profile_label": "instance_name",
        "vm_profile_label_type": "nova_internal_name",
        "vm_profile_fallback": "skip",
        "threshold": 0.15,
        "cooldown": "10m",
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_instance(
    uuid: str = "uuid-1",
    name: str = "vm-1",
    internal_name: str = "instance-00001",
    host: str = "h1",
    vcpus: int = 4,
) -> Instance:
    return Instance(
        uuid=uuid,
        name=name,
        internal_name=internal_name,
        host=host,
        flavor_vcpus=vcpus,
        flavor_ram_mb=8192,
        status="ACTIVE",
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
    def test_basic_collection(
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
        profiles = profiler.collect(policy, ["h1", "h2"], {"h1": 0.4, "h2": 0.8})

        assert len(profiles) == 2
        assert profiles["u1"].weight == pytest.approx(0.3)
        assert profiles["u2"].weight == pytest.approx(0.7)
        assert profiles["u1"].source == "prometheus"

    def test_no_instances(
        self, profiler: VmProfiler, mock_nova: MagicMock,
    ) -> None:
        mock_nova.list_instances_on_host.return_value = []
        policy = _make_policy()
        profiles = profiler.collect(policy, ["h1"], {"h1": 0.5})
        assert profiles == {}

    def test_skip_fallback_excludes_vm(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [
            _make_instance(uuid="u1", internal_name="inst-001", host="h1"),
            _make_instance(uuid="u2", internal_name="inst-002", host="h1"),
        ]
        mock_nova.list_instances_on_host.return_value = instances
        # Only one VM has Prometheus data
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"inst-001": 0.5},
        )

        policy = _make_policy(vm_profile_fallback="skip")
        profiles = profiler.collect(policy, ["h1"], {"h1": 0.6})

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
        profiles = profiler.collect(policy, ["h1"], {"h1": 0.8})

        assert len(profiles) == 2
        assert profiles["u1"].source == "prometheus"
        assert profiles["u2"].source == "fallback:host_average"
        assert profiles["u2"].weight == pytest.approx(0.4)  # 0.8 / 2

    def test_no_vm_profile_query(
        self, profiler: VmProfiler, mock_nova: MagicMock, mock_prometheus: MagicMock,
    ) -> None:
        instances = [_make_instance(uuid="u1", internal_name="inst-001", host="h1")]
        mock_nova.list_instances_on_host.return_value = instances

        policy = _make_policy(vm_profile_query=None, vm_profile_fallback="host_average")
        profiles = profiler.collect(policy, ["h1"], {"h1": 0.6})

        assert len(profiles) == 1
        assert profiles["u1"].source == "fallback:host_average"
        mock_prometheus.instant_query.assert_not_called()

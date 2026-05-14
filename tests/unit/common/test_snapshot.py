"""Tests for the shared snapshot writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kronos.clients.nova import ComputeService, Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.snapshot import SNAPSHOT_SUBDIR_PREFIX, write_snapshot
from kronos.policies.models import PoliciesConfig, PolicyConfig


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "p1",
        "mode": "spread",
        "weight": 1.0,
        "imbalance_query": "imbalance_metric",
        "vm_profile_query": "vm_metric",
        "vm_profile_label": "instance_id",
        "vm_profile_label_type": "nova_instance_uuid",
        "vm_profile_fallback": "skip",
        "threshold": 0.15,
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_query_result(query: str, series: dict[str, float]) -> QueryResult:
    return QueryResult(
        query=query,
        timestamp=datetime.now(tz=UTC),
        health=PrometheusHealth.HEALTHY,
        series=series,
        missing_labels=set(),
        warnings=[],
    )


@pytest.fixture()
def nova() -> MagicMock:
    n = MagicMock()
    n.get_hosts_in_aggregate.side_effect = lambda agg: (
        ["h1", "h2"] if agg == "agg-a" else []
    )
    n.list_instances_on_host.side_effect = lambda host: [
        Instance(
            uuid=f"u-{host}",
            name=f"vm-{host}",
            internal_name=f"i-{host}",
            host=host,
            flavor_vcpus=2,
            flavor_ram_mb=4096,
            status="ACTIVE",
        ),
    ]
    n.list_server_groups.return_value = [{"id": "g1", "policy": "affinity"}]
    n.list_compute_services.return_value = [
        ComputeService(
            host="h1", binary="nova-compute",
            state="up", status="enabled", zone="nova",
        ),
        ComputeService(
            host="h2", binary="nova-compute",
            state="up", status="enabled", zone="nova",
        ),
    ]
    return n


@pytest.fixture()
def prometheus() -> MagicMock:
    p = MagicMock()
    p.instant_query.side_effect = lambda query, label_key: _make_query_result(
        query, {"h1": 0.4, "h2": 0.6},
    )
    return p


def test_creates_timestamped_subdir(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a"],
    )
    assert target.parent == tmp_path
    assert target.name.startswith(SNAPSHOT_SUBDIR_PREFIX)
    assert target.is_dir()


def test_creates_parent_when_missing(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    parent = tmp_path / "snapshots" / "today"
    target = write_snapshot(
        parent,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a"],
    )
    assert parent.is_dir()
    assert target.parent == parent


def test_writes_expected_files(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a"],
    )
    assert (target / "meta.json").exists()
    assert (target / "cooldowns.json").exists()
    assert (target / "nova" / "aggregates.json").exists()
    assert (target / "nova" / "instances.json").exists()
    assert (target / "nova" / "server_groups.json").exists()
    assert (target / "nova" / "services.json").exists()
    assert (target / "prometheus" / "imbalance_p1.json").exists()
    assert (target / "prometheus" / "vm_profile_p1.json").exists()


def test_unassigned_aggregate_uses_marker(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a", None],
    )
    meta = json.loads((target / "meta.json").read_text())
    assert "_unassigned_" in meta["aggregates"]


def test_skips_disabled_policies(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[
            _make_policy(name="active"),
            _make_policy(name="inactive", enabled=False),
        ]),
        ["agg-a"],
    )
    assert (target / "prometheus" / "imbalance_active.json").exists()
    assert not (target / "prometheus" / "imbalance_inactive.json").exists()


def test_section_failure_does_not_abort_snapshot(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    nova.list_server_groups.side_effect = Exception("API down")
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a"],
    )
    assert (target / "nova" / "server_groups.json").read_text() == "[]"
    assert (target / "meta.json").exists()


def test_services_round_trip_includes_zone(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    """Services written by the snapshot writer must carry the AZ.

    Replay's host-AZ filter relies on this round-trip - without the
    zone field, every host would be dropped from the engine's scope.
    """
    nova.list_compute_services.return_value = [
        ComputeService(
            host="h1", binary="nova-compute",
            state="up", status="enabled", zone="gpu-az",
        ),
        ComputeService(
            host="h2", binary="nova-compute",
            state="up", status="enabled", zone="cpu-az",
        ),
    ]
    target = write_snapshot(
        tmp_path,
        nova,
        prometheus,
        PoliciesConfig(policies=[_make_policy()]),
        ["agg-a"],
    )
    services = json.loads((target / "nova" / "services.json").read_text())
    by_host = {s["host"]: s for s in services}
    assert by_host["h1"]["zone"] == "gpu-az"
    assert by_host["h2"]["zone"] == "cpu-az"


def test_returns_unique_subdirs_across_calls(
    tmp_path: Path, nova: MagicMock, prometheus: MagicMock,
) -> None:
    a = write_snapshot(
        tmp_path, nova, prometheus,
        PoliciesConfig(policies=[_make_policy()]), ["agg-a"],
    )
    # Force a different timestamp by sleeping past one-second resolution.
    import time
    time.sleep(1.1)
    b = write_snapshot(
        tmp_path, nova, prometheus,
        PoliciesConfig(policies=[_make_policy()]), ["agg-a"],
    )
    assert a != b
    assert a.is_dir() and b.is_dir()

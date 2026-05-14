"""Tests for the kronos-replay snapshot reader stubs.

The CLI itself is exercised end-to-end via the engine's run_once()
path (covered in tests/unit/engine/test_loop.py).  These tests cover
the snapshot-shaped behaviour that is unique to the replay stubs:
zone defaults for old snapshots, aggregate key resolution, and the
unassigned-pool marker.
"""

from __future__ import annotations

import json
from pathlib import Path

from kronos.cmd.replay import ReplayNovaClient


def _write_minimal_snapshot(
    snapshot_dir: Path,
    *,
    services_payload: list[dict[str, object]] | None,
    aggregates: dict[str, list[str]] | None = None,
) -> None:
    """Write the four nova/*.json files a replay needs.

    ``services_payload=None`` omits the services.json file entirely,
    simulating an older snapshot that pre-dates the host-liveness gate.
    """
    nova_dir = snapshot_dir / "nova"
    nova_dir.mkdir(parents=True)
    if aggregates is None:
        aggregates = {"agg-a": ["h1", "h2"]}
    (nova_dir / "aggregates.json").write_text(json.dumps(aggregates))
    (nova_dir / "instances.json").write_text("{}")
    (nova_dir / "server_groups.json").write_text("[]")
    if services_payload is not None:
        (nova_dir / "services.json").write_text(
            json.dumps(services_payload),
        )


def test_zone_round_trips_from_services_json(tmp_path: Path) -> None:
    _write_minimal_snapshot(
        tmp_path,
        services_payload=[
            {
                "host": "h1", "binary": "nova-compute",
                "state": "up", "status": "enabled", "zone": "gpu-az",
            },
            {
                "host": "h2", "binary": "nova-compute",
                "state": "up", "status": "enabled", "zone": "cpu-az",
            },
        ],
    )

    client = ReplayNovaClient(tmp_path, default_zone="nova")
    svcs = {s.host: s for s in client.list_compute_services()}
    assert svcs["h1"].zone == "gpu-az"
    assert svcs["h2"].zone == "cpu-az"


def test_missing_zone_falls_back_to_default(tmp_path: Path) -> None:
    """Older snapshots without ``zone`` use the engine's configured AZ.

    Engine's AZ filter compares ``svc.zone == conf.availability_zone``;
    if the field is absent we synthesise the configured AZ so the
    filter doesn't drop every host.
    """
    _write_minimal_snapshot(
        tmp_path,
        services_payload=[
            {
                "host": "h1", "binary": "nova-compute",
                "state": "up", "status": "enabled",
                # No ``zone`` key.
            },
        ],
    )

    client = ReplayNovaClient(tmp_path, default_zone="my-az")
    assert client.list_compute_services()[0].zone == "my-az"


def test_missing_services_file_synthesises_default_zone(tmp_path: Path) -> None:
    """Pre-host-liveness snapshots lack services.json entirely."""
    _write_minimal_snapshot(
        tmp_path,
        services_payload=None,
        aggregates={"agg-a": ["h1", "h2"]},
    )
    client = ReplayNovaClient(tmp_path, default_zone="my-az")
    services = client.list_compute_services()
    assert sorted(s.host for s in services) == ["h1", "h2"]
    assert all(s.zone == "my-az" for s in services)
    assert all(s.is_available_destination for s in services)


def test_aggregate_keys_resolves_unassigned_to_none(tmp_path: Path) -> None:
    _write_minimal_snapshot(
        tmp_path,
        services_payload=[],
        aggregates={"agg-a": ["h1"], "_unassigned_": ["h2"]},
    )
    client = ReplayNovaClient(tmp_path)
    assert set(client.aggregate_keys()) == {"agg-a", None}

"""Entry point for ``kronos-replay``: run the engine against recorded data.

Loads a snapshot directory produced by ``kronos-record`` (or by the
engine's SIGUSR1 handler) and runs exactly one combined-scoring cycle
offline.  The CLI is a thin wrapper: it builds replay-stub clients
backed by the snapshot files, dependency-injects them into
``EngineLoop``, and invokes the same ``run_once()`` the daemon's main
loop calls every interval.  All evaluation, profiling, evacuation,
affinity enforcement, planning, and logging code is shared with the
production engine - there is no parallel pipeline here to drift.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import ComputeService, Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.config import register_opts
from kronos.common.exceptions import AggregateNotFound
from kronos.common.messaging import UNASSIGNED_TOPIC_MARKER
from kronos.engine.cooldown import CooldownTracker
from kronos.engine.loop import EngineLoop

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class ReplayNovaClient:
    """Nova client stub that reads from a snapshot directory."""

    def __init__(
        self,
        snapshot_dir: Path,
        default_zone: str = "nova",
    ) -> None:
        nova_dir = snapshot_dir / "nova"
        self._aggregates: dict[str, list[str]] = json.loads(
            (nova_dir / "aggregates.json").read_text(),
        )
        self._instances: dict[str, list[dict[str, object]]] = json.loads(
            (nova_dir / "instances.json").read_text(),
        )
        self._server_groups: list[dict[str, object]] = json.loads(
            (nova_dir / "server_groups.json").read_text(),
        )

        # nova-compute service state - optional for backwards compat
        # with older snapshots that pre-date the host-liveness gate.
        # When the file is missing, synthesise an "all up + enabled"
        # map covering every host we know about so the replay still
        # produces destinations.  ``default_zone`` is what the engine's
        # AZ filter compares against; old snapshots without a ``zone``
        # field default to it so the filter doesn't drop every host.
        services_path = nova_dir / "services.json"
        if services_path.exists():
            self._services: list[ComputeService] = [
                ComputeService(
                    host=str(d.get("host", "")),
                    binary=str(d.get("binary", "nova-compute")),
                    state=str(d.get("state", "up")),
                    status=str(d.get("status", "enabled")),
                    zone=str(d.get("zone", default_zone)),
                    disabled_reason=str(d.get("disabled_reason", "")),
                    forced_down=bool(d.get("forced_down", False)),
                )
                for d in json.loads(services_path.read_text())
            ]
        else:
            all_hosts: set[str] = set()
            for hosts in self._aggregates.values():
                all_hosts.update(hosts)
            self._services = [
                ComputeService(
                    host=h,
                    binary="nova-compute",
                    state="up",
                    status="enabled",
                    zone=default_zone,
                )
                for h in sorted(all_hosts)
            ]

    def aggregate_keys(self) -> list[str | None]:
        """Return the aggregate scope recorded in the snapshot."""
        keys: list[str | None] = []
        for key in self._aggregates:
            keys.append(None if key == UNASSIGNED_TOPIC_MARKER else key)
        return keys

    def get_hosts_in_aggregate(
        self, aggregate_name: str | None,
    ) -> list[str]:
        key = aggregate_name if aggregate_name is not None else UNASSIGNED_TOPIC_MARKER
        hosts = self._aggregates.get(key)
        if hosts is None:
            raise AggregateNotFound(aggregate=key)
        return hosts

    def list_instances_on_host(self, host: str) -> list[Instance]:
        raw = self._instances.get(host, [])
        instances: list[Instance] = []
        for r in raw:
            sg = r.get("server_groups")
            instances.append(
                Instance(
                    uuid=str(r.get("uuid", "")),
                    name=str(r.get("name", "")),
                    internal_name=str(r.get("internal_name", "")),
                    host=str(r.get("host", host)),
                    flavor_vcpus=int(str(r.get("flavor_vcpus", 0))),
                    flavor_ram_mb=int(str(r.get("flavor_ram_mb", 0))),
                    status=str(r.get("status", "ACTIVE")),
                    server_groups=list(sg) if isinstance(sg, list) else [],
                ),
            )
        return instances

    def list_server_groups(self) -> list[dict[str, object]]:
        return self._server_groups

    def list_compute_services(self) -> list[ComputeService]:
        return list(self._services)


class ReplayPrometheusClient:
    """Prometheus client stub that reads from a snapshot directory."""

    def __init__(self, snapshot_dir: Path) -> None:
        self._prom_dir = snapshot_dir / "prometheus"

    def instant_query(
        self,
        query: str,
        label_key: str = "host",
        expected_labels: set[str] | None = None,
    ) -> QueryResult:
        """Look up a recorded query result by matching the query string."""
        for path in sorted(self._prom_dir.glob("*.json")):
            data = json.loads(path.read_text())
            if data.get("query") == query:
                series: dict[str, float] = data.get("series", {})
                missing: set[str] = set()
                health = PrometheusHealth(data.get("health", "healthy"))

                if expected_labels is not None:
                    missing = expected_labels - series.keys()
                    if missing:
                        health = PrometheusHealth.PARTIAL

                return QueryResult(
                    query=query,
                    timestamp=datetime.now(tz=UTC),
                    health=health,
                    series=series,
                    missing_labels=missing,
                    warnings=data.get("warnings", []),
                )

        LOG.warning("No recorded data for query: %s", query[:80])
        return QueryResult(
            query=query,
            timestamp=datetime.now(tz=UTC),
            health=PrometheusHealth.UNREACHABLE,
            series={},
            warnings=["No recorded data for this query"],
        )


def _load_cooldowns(
    snapshot_dir: Path,
    tracker: CooldownTracker,
) -> None:
    """Seed the tracker from ``<snapshot>/cooldowns.json`` if present.

    File shape:

    ```json
    {
      "aggregate_cooldowns":  {"gpu": 120},
      "instance_cooldowns":   {"vm-abc": 300},
      "instance_quarantines": {"vm-xyz": 1800, "vm-banned": -1}
    }
    ```

    All sections are optional.  Values are seconds remaining at replay
    time; ``-1`` in ``instance_quarantines`` means indefinite.
    """
    path = snapshot_dir / "cooldowns.json"
    if not path.exists():
        return

    data = json.loads(path.read_text())

    for aggregate, remaining in data.get("aggregate_cooldowns", {}).items():
        tracker.seed_aggregate_cooldown(aggregate, float(remaining))
    for instance_uuid, remaining in data.get("instance_cooldowns", {}).items():
        tracker.seed_instance_cooldown(instance_uuid, float(remaining))
    for instance_uuid, remaining in data.get(
        "instance_quarantines", {},
    ).items():
        tracker.seed_instance_quarantine(instance_uuid, float(remaining))

    LOG.info(
        "Seeded cooldowns from %s: %d aggregate, %d instance, %d quarantine",
        path,
        len(data.get("aggregate_cooldowns", {})),
        len(data.get("instance_cooldowns", {})),
        len(data.get("instance_quarantines", {})),
    )


def _log_timings(timings: dict[str, float], total: float) -> None:
    """Print per-phase wall-clock totals collected by the engine."""
    LOG.info("--- Timings ---")
    phases = ("scorer", "profiler", "evacuator", "enforcer", "planner")
    for phase in phases:
        seconds = timings.get(phase, 0.0)
        pct = (seconds / total * 100.0) if total else 0.0
        LOG.info("%s %.3fs (%.1f%%)", phase, seconds, pct)
    LOG.info("total %.3fs (100.0%%)", total)


def main() -> int:
    """Main entry point for kronos-replay."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF.register_cli_opt(
        cfg.StrOpt(
            "snapshot-dir",
            positional=True,
            help="Path to snapshot directory from kronos-record.",
        ),
    )
    CONF.register_cli_opt(
        cfg.BoolOpt(
            "time",
            default=False,
            help=(
                "Print per-phase wall-clock timings (scorer, profiler, "
                "evacuator, enforcer, planner) at the end of the cycle."
            ),
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-replay",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-replay")

    snapshot_dir = Path(CONF["snapshot_dir"])
    if not snapshot_dir.is_dir():
        LOG.error("Snapshot directory does not exist: %s", snapshot_dir)
        return 1

    meta_file = snapshot_dir / "meta.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        LOG.info(
            "Replaying snapshot from %s (%d policies)",
            meta.get("recorded_at", "unknown"),
            len(meta.get("policy_names", [])),
        )

    nova = ReplayNovaClient(
        snapshot_dir,
        default_zone=CONF.engine.availability_zone,
    )
    prometheus = ReplayPrometheusClient(snapshot_dir)

    cooldown = CooldownTracker(
        aggregate_cooldown_seconds=float(CONF.engine.cooldown),
        instance_cooldown_seconds=float(CONF.engine.instance_cooldown),
    )
    _load_cooldowns(snapshot_dir, cooldown)

    timings: dict[str, float] | None = {} if CONF["time"] else None

    engine = EngineLoop(
        CONF,
        nova=nova,  # type: ignore[arg-type]
        prometheus=prometheus,  # type: ignore[arg-type]
        cooldown=cooldown,
        timings=timings,
    )

    started = time.perf_counter()
    report = engine.run_once(aggregates=nova.aggregate_keys())
    total = time.perf_counter() - started

    engine._log_report(report)

    if timings is not None:
        _log_timings(timings, total)

    return 0

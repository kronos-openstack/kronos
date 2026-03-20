"""Entry point for ``kronos-replay``: run the engine against recorded data.

Loads a snapshot directory produced by ``kronos-record`` and runs a
single engine evaluation cycle against it.  Nova and Prometheus clients
are replaced with replay stubs that serve data from the snapshot files.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.config import register_opts
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import CycleReport, MigrationPlan
from kronos.policies.loader import load_policies
from kronos.policies.models import PolicyConfig

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class ReplayNovaClient:
    """Nova client stub that reads from a snapshot directory."""

    def __init__(self, snapshot_dir: Path) -> None:
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

    def get_aggregate_hosts(self, aggregate_name: str) -> list[str]:
        hosts = self._aggregates.get(aggregate_name)
        if hosts is None:
            from kronos.common.exceptions import AggregateNotFound
            raise AggregateNotFound(aggregate=aggregate_name)
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
        """Look up a recorded query result by matching files.

        The recorder writes one file per (policy, query_type) pair.
        We match by scanning all files for the one whose ``query``
        field matches the requested query string.
        """
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


def _run_replay(
    snapshot_dir: Path,
    policies_file: str,
) -> int:
    """Run a single engine cycle against snapshot data."""
    nova = ReplayNovaClient(snapshot_dir)
    prometheus = ReplayPrometheusClient(snapshot_dir)

    scorer = PolicyScorer(prometheus, nova)  # type: ignore[arg-type]
    profiler = VmProfiler(prometheus, nova)  # type: ignore[arg-type]
    constraints = ConstraintChecker(nova)  # type: ignore[arg-type]
    planner = Planner(constraints)

    policies = load_policies(policies_file)
    started_at = datetime.now(tz=UTC)

    report = CycleReport(
        cycle_number=1,
        started_at=started_at,
        completed_at=started_at,
        dry_run=True,
    )

    for policy in policies.policies:
        if not policy.enabled:
            continue

        LOG.info("=== Policy: %s (mode=%s) ===", policy.name, policy.mode.value)

        try:
            result = scorer.evaluate(policy)
        except Exception as exc:
            LOG.error("Scoring failed: %s", exc)
            report.errors.append(f"{policy.name}: {exc}")
            continue

        if result.skipped:
            LOG.info("  SKIPPED: %s", result.skip_reason)
            report.policy_results.append(result)
            continue

        LOG.info(
            "  Imbalance: %.3f (threshold=%.3f, detected=%s)",
            result.imbalance,
            policy.threshold,
            result.imbalance_detected,
        )
        for hs in result.host_scores:
            LOG.info(
                "    %s: raw=%.4f normalized=%.3f",
                hs.host,
                hs.raw_score,
                hs.normalized_score,
            )

        if result.imbalance_detected:
            raw_scores = {hs.host: hs.raw_score for hs in result.host_scores}
            aggregate_hosts = list(raw_scores.keys())

            vm_profiles = profiler.collect(
                policy=policy,
                aggregate_hosts=aggregate_hosts,
                host_scores=raw_scores,
            )
            result.vm_profiles = vm_profiles

            if vm_profiles:
                plan = planner.plan(
                    policy=policy,
                    host_scores=result.host_scores,
                    vm_profiles=vm_profiles,
                )
                result.migration_plan = plan
                _log_plan(policy, plan)
            else:
                LOG.info("  No VM profiles collected — cannot plan.")

        report.policy_results.append(result)

    report.completed_at = datetime.now(tz=UTC)
    duration = (report.completed_at - report.started_at).total_seconds()

    LOG.info(
        "=== Replay complete: %d policies, %d errors, %.1fs ===",
        len(report.policy_results),
        len(report.errors),
        duration,
    )
    return 0


def _log_plan(policy: PolicyConfig, plan: MigrationPlan) -> None:
    """Log a migration plan in detail."""
    if not plan.steps:
        LOG.info("  No migrations proposed.")
        return

    LOG.info(
        "  Migration plan: %d steps, imbalance %.3f -> %.3f",
        plan.migration_count,
        plan.initial_imbalance,
        plan.projected_imbalance,
    )
    for i, step in enumerate(plan.steps, 1):
        LOG.info(
            "    [%d] %s (%s): %s -> %s (weight=%.4f, improvement=%.4f)",
            i,
            step.instance_name,
            step.instance_uuid[:8],
            step.from_host,
            step.to_host,
            step.weight,
            step.improvement,
        )


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

    return _run_replay(snapshot_dir, CONF.engine.policies_file)

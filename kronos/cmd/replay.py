"""Entry point for ``kronos-replay``: run the engine against recorded data.

Loads a snapshot directory produced by ``kronos-record`` and runs one
combined-scoring evaluation per aggregate offline.  Nova and Prometheus
clients are replaced with replay stubs backed by the snapshot files.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.config import register_opts
from kronos.common.exceptions import AggregateNotFound
from kronos.common.messaging import UNASSIGNED_TOPIC_MARKER
from kronos.engine.affinity_enforcer import AffinityEnforcer
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import MigrationPlan
from kronos.policies.loader import load_policies

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


def _run_replay(
    snapshot_dir: Path,
    policies_file: str,
    *,
    enforce_hard: bool,
    enforce_soft: bool,
    show_timings: bool,
) -> int:
    """Run a single engine cycle against snapshot data."""
    nova = ReplayNovaClient(snapshot_dir)
    prometheus = ReplayPrometheusClient(snapshot_dir)

    scorer = PolicyScorer(prometheus)  # type: ignore[arg-type]
    profiler = VmProfiler(prometheus, nova)  # type: ignore[arg-type]
    constraints = ConstraintChecker(nova)  # type: ignore[arg-type]
    enforcer = AffinityEnforcer(
        constraints,
        enforce_hard=enforce_hard,
        enforce_soft=enforce_soft,
    )
    planner = Planner(constraints)

    policies = load_policies(policies_file)
    enabled = [p for p in policies.policies if p.enabled]

    # Aggregates to iterate: mirror the snapshot's keys.
    aggregates: list[str | None] = []
    for key in nova._aggregates:
        aggregates.append(None if key == UNASSIGNED_TOPIC_MARKER else key)

    totals: dict[str, float] = {
        "scorer": 0.0,
        "profiler": 0.0,
        "enforcer": 0.0,
        "planner": 0.0,
    }
    cycle_start = time.perf_counter()

    for aggregate in aggregates:
        name = aggregate if aggregate is not None else "<unassigned>"
        LOG.info("=== Aggregate: %s ===", name)

        hosts = nova.get_hosts_in_aggregate(aggregate)
        if not hosts:
            LOG.info("  No hosts recorded, skipping.")
            continue

        t0 = time.perf_counter()
        policy_results = [scorer.evaluate(p, hosts) for p in enabled]
        totals["scorer"] += time.perf_counter() - t0

        combined_imbalance = 0.0
        any_detected = False
        name_width = max(
            (len(pr.policy_name) for pr in policy_results),
            default=0,
        )
        for policy, pr in zip(enabled, policy_results, strict=True):
            if pr.skipped:
                LOG.info(
                    "  policy %-*s skipped (%s)",
                    name_width, pr.policy_name, pr.skip_reason,
                )
                continue
            combined_imbalance += policy.weight * pr.imbalance
            if pr.imbalance_detected:
                any_detected = True
            suffix = " (threshold exceeded)" if pr.imbalance_detected else ""
            LOG.info(
                "  policy %-*s imbalance %.3f (threshold %.3f)%s",
                name_width, pr.policy_name, pr.imbalance,
                policy.threshold, suffix,
            )

        LOG.info("  Combined imbalance: %.3f", combined_imbalance)

        if not any_detected and not enforcer.enabled:
            continue

        active = [
            (p, pr) for p, pr in zip(enabled, policy_results, strict=True)
            if not pr.skipped
        ]
        if not active:
            LOG.info("  No active policies, cannot plan.")
            continue
        active_policies = [p for p, _ in active]
        active_results = [pr for _, pr in active]
        host_scores_by_policy = {
            p.name: {hs.host: hs.raw_score for hs in pr.host_scores}
            for p, pr in active
        }

        t0 = time.perf_counter()
        vm_profiles = profiler.collect(
            policies=active_policies,
            hosts=hosts,
            host_scores_by_policy=host_scores_by_policy,
        )
        totals["profiler"] += time.perf_counter() - t0

        if not vm_profiles:
            LOG.info("  No VM profiles collected, cannot plan.")
            continue

        budget = max(p.max_migrations_per_cycle for p in active_policies)

        t0 = time.perf_counter()
        plan, scores, vms_by_host, remaining = enforcer.enforce(
            aggregate=name,
            policies=active_policies,
            policy_results=active_results,
            vm_profiles=vm_profiles,
            budget=budget,
        )
        totals["enforcer"] += time.perf_counter() - t0

        if any_detected:
            t0 = time.perf_counter()
            plan = planner.plan(
                aggregate=name,
                policies=active_policies,
                policy_results=active_results,
                vm_profiles=vm_profiles,
                scores=scores,
                vms_by_host=vms_by_host,
                budget=remaining,
                plan=plan,
            )
            totals["planner"] += time.perf_counter() - t0

        _log_plan(plan)

    if show_timings:
        total = time.perf_counter() - cycle_start
        LOG.info("--- Timings ---")
        for phase, seconds in totals.items():
            pct = (seconds / total * 100.0) if total else 0.0
            LOG.info("  %-10s %.3fs  (%.1f%%)", phase, seconds, pct)
        LOG.info("  %-10s %.3fs  (100.0%%)", "total", total)

    return 0


def _log_plan(plan: MigrationPlan) -> None:
    if not plan.steps:
        LOG.info("  No migrations proposed.")
        return

    LOG.info(
        "  Plan: %d moves, combined imbalance %.3f -> %.3f (projected)",
        plan.migration_count,
        plan.initial_imbalance,
        plan.projected_imbalance,
    )
    for i, step in enumerate(plan.steps, 1):
        LOG.info(
            "    %d. %-8s %s (%s) %s -> %s, gain %.4f",
            i,
            step.phase.value,
            step.instance_name,
            step.instance_uuid[:8],
            step.from_host,
            step.to_host,
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
    CONF.register_cli_opt(
        cfg.BoolOpt(
            "time",
            default=False,
            help=(
                "Print per-phase wall-clock timings (scorer, profiler, "
                "enforcer, planner) at the end of the cycle."
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

    return _run_replay(
        snapshot_dir,
        CONF.engine.policies_file,
        enforce_hard=CONF.engine.enforce_hard_affinity,
        enforce_soft=CONF.engine.enforce_soft_affinity,
        show_timings=CONF["time"],
    )

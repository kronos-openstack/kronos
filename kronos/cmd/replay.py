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

from kronos.clients.nova import ComputeService, Instance
from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.config import register_opts
from kronos.common.exceptions import AggregateNotFound
from kronos.common.messaging import UNASSIGNED_TOPIC_MARKER
from kronos.engine._sim import combined_imbalance, group_vms_by_host
from kronos.engine.affinity_enforcer import AffinityEnforcer
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.cooldown import CooldownTracker
from kronos.engine.evacuator import Evacuator
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import MigrationPlan, VmProfile
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

        # nova-compute service state - optional for backwards compat
        # with older snapshots that pre-date the host-liveness gate.
        # When the file is missing, synthesise an "all up + enabled"
        # map covering every host we know about so the replay still
        # produces destinations.
        services_path = nova_dir / "services.json"
        if services_path.exists():
            self._services: list[ComputeService] = [
                ComputeService(
                    host=str(d.get("host", "")),
                    binary=str(d.get("binary", "nova-compute")),
                    state=str(d.get("state", "up")),
                    status=str(d.get("status", "enabled")),
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
                )
                for h in sorted(all_hosts)
            ]

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


def _run_replay(
    snapshot_dir: Path,
    policies_file: str,
    *,
    enforce_hard: bool,
    enforce_soft: bool,
    evacuate_disabled: bool,
    show_timings: bool,
    cooldown: CooldownTracker,
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
    evacuator = Evacuator(constraints, enabled=evacuate_disabled)
    planner = Planner(constraints)

    services = {svc.host: svc for svc in nova.list_compute_services()}
    constraints.set_services(services)

    policies = load_policies(policies_file)
    enabled = [p for p in policies.policies if p.enabled]

    # Aggregates to iterate: mirror the snapshot's keys.
    aggregates: list[str | None] = []
    for key in nova._aggregates:
        aggregates.append(None if key == UNASSIGNED_TOPIC_MARKER else key)

    totals: dict[str, float] = {
        "scorer": 0.0,
        "profiler": 0.0,
        "evacuator": 0.0,
        "enforcer": 0.0,
        "planner": 0.0,
    }
    cycle_start = time.perf_counter()

    for aggregate in aggregates:
        name = aggregate if aggregate is not None else "<unassigned>"
        LOG.info("=== Aggregate: %s ===", name)

        hosts = nova.get_hosts_in_aggregate(aggregate)
        if not hosts:
            LOG.info("No hosts recorded, skipping.")
            continue

        if cooldown.is_aggregate_cooling(name):
            LOG.info("Aggregate '%s' is cooling, skipping planning.", name)
            continue

        t0 = time.perf_counter()
        policy_results = [scorer.evaluate(p, hosts) for p in enabled]
        totals["scorer"] += time.perf_counter() - t0

        weighted_imbalance = 0.0
        any_detected = False
        for policy, pr in zip(enabled, policy_results, strict=True):
            if pr.skipped:
                LOG.info(
                    "policy %s skipped (%s)",
                    pr.policy_name, pr.skip_reason,
                )
                continue
            weighted_imbalance += policy.weight * pr.imbalance
            if pr.imbalance_detected:
                any_detected = True
            suffix = " (threshold exceeded)" if pr.imbalance_detected else ""
            LOG.info(
                "policy %s imbalance %.3f (threshold %.3f)%s",
                pr.policy_name, pr.imbalance,
                policy.threshold, suffix,
            )

        LOG.info("Combined imbalance: %.3f", weighted_imbalance)

        if not any_detected and not enforcer.enabled and not evacuator.enabled:
            continue

        active = [
            (p, pr) for p, pr in zip(enabled, policy_results, strict=True)
            if not pr.skipped
        ]
        if not active:
            LOG.info("No active policies, cannot plan.")
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

        vm_profiles = _filter_unavailable(vm_profiles, cooldown, name)

        if not vm_profiles:
            LOG.info("No VM profiles collected, cannot plan.")
            continue

        budget = max(p.max_migrations_per_cycle for p in active_policies)

        scores = {
            p.name: {hs.host: hs.raw_score for hs in pr.host_scores}
            for p, pr in active
        }
        vms_by_host = group_vms_by_host(vm_profiles)

        plan = MigrationPlan(
            aggregate=name,
            policy_names=[p.name for p in active_policies],
            initial_imbalance=combined_imbalance(scores, active_policies),
        )

        t0 = time.perf_counter()
        evac_plan, scores, vms_by_host, remaining = evacuator.evacuate(
            aggregate=name,
            aggregate_hosts=hosts,
            policies=active_policies,
            scores=scores,
            vms_by_host=vms_by_host,
            vm_profiles=vm_profiles,
            services=services,
            budget=budget,
        )
        plan.steps.extend(evac_plan.steps)
        totals["evacuator"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        enforce_plan, scores, vms_by_host, remaining = enforcer.enforce(
            aggregate=name,
            policies=active_policies,
            policy_results=active_results,
            vm_profiles=vm_profiles,
            budget=remaining,
            scores=scores,
            vms_by_host=vms_by_host,
        )
        plan.steps.extend(enforce_plan.steps)
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

        plan.projected_imbalance = combined_imbalance(scores, active_policies)
        _log_plan(plan)

    if show_timings:
        total = time.perf_counter() - cycle_start
        LOG.info("--- Timings ---")
        for phase, seconds in totals.items():
            pct = (seconds / total * 100.0) if total else 0.0
            LOG.info("%s %.3fs (%.1f%%)", phase, seconds, pct)
        LOG.info("%s %.3fs (100.0%%)", "total", total)

    return 0


def _filter_unavailable(
    vm_profiles: dict[str, VmProfile],
    cooldown: CooldownTracker,
    aggregate: str,
) -> dict[str, VmProfile]:
    """Drop VMs currently in instance cooldown or quarantine."""
    kept: dict[str, VmProfile] = {}
    skipped: list[str] = []
    for instance_uuid, profile in vm_profiles.items():
        if cooldown.is_instance_quarantined(instance_uuid):
            skipped.append(f"{instance_uuid}=quarantined")
            continue
        if cooldown.is_instance_cooling(instance_uuid):
            skipped.append(f"{instance_uuid}=cooling")
            continue
        kept[instance_uuid] = profile
    if skipped:
        LOG.info(
            "Excluded %d VM(s) from planning in '%s': %s",
            len(skipped),
            aggregate,
            ", ".join(skipped),
        )
    return kept


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


def _log_plan(plan: MigrationPlan) -> None:
    if not plan.steps:
        LOG.info("No migrations proposed.")
        return

    LOG.info(
        "Plan: %d moves, combined imbalance %.3f -> %.3f (projected)",
        plan.migration_count,
        plan.initial_imbalance,
        plan.projected_imbalance,
    )
    for i, step in enumerate(plan.steps, 1):
        LOG.info(
            "%d. %s %s (%s) %s -> %s, gain %.4f",
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

    cooldown = CooldownTracker(
        aggregate_cooldown_seconds=float(CONF.engine.cooldown),
        instance_cooldown_seconds=float(CONF.engine.instance_cooldown),
    )
    _load_cooldowns(snapshot_dir, cooldown)

    return _run_replay(
        snapshot_dir,
        CONF.engine.policies_file,
        enforce_hard=CONF.engine.enforce_hard_affinity,
        enforce_soft=CONF.engine.enforce_soft_affinity,
        evacuate_disabled=CONF.engine.evacuate_disabled_hosts,
        show_timings=CONF["time"],
        cooldown=cooldown,
    )

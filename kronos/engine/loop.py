"""Main engine control loop.

The engine operates on a fixed set of aggregates defined at startup via
``[engine] aggregates`` and ``[engine] include_unassigned_hosts``.  Each
aggregate is evaluated independently every cycle:

1. resolve aggregate → host list via Nova
2. run every enabled policy against that host list (scorer)
3. collect VM profiles for all policies in one pass (profiler)
4. combined-scoring planner produces at most one migration plan
5. cast migration tasks over RPC to the executor (unless dry_run)
"""

from __future__ import annotations

import signal
import time
import uuid
from datetime import UTC, datetime
from types import FrameType

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.messaging import (
    get_rpc_client,
    get_rpc_transport,
    migrations_topic,
    task_to_dict,
)
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.cooldown import CooldownTracker
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import (
    AggregateResult,
    CycleReport,
    MigrationPlan,
    MigrationTask,
    PolicyResult,
)
from kronos.policies.loader import load_policies
from kronos.policies.models import PoliciesConfig, PolicyConfig

LOG = logging.getLogger(__name__)


class EngineLoop:
    """Periodic per-aggregate evaluation loop with combined scoring."""

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        self._conf = conf
        self._prometheus = PrometheusClient(conf)
        self._nova = NovaClient(conf)
        self._scorer = PolicyScorer(self._prometheus)
        self._profiler = VmProfiler(self._prometheus, self._nova)
        self._constraints = ConstraintChecker(self._nova)
        self._planner = Planner(self._constraints)
        self._cooldown = CooldownTracker(
            aggregate_cooldown_seconds=float(conf.engine.cooldown),
            instance_cooldown_seconds=float(conf.engine.instance_cooldown),
        )
        self._running = False
        self._cycle_count = 0

        # Messaging — only initialised when dry_run=False.
        # Keys: aggregate name, or None for the unassigned-hosts pool.
        self._rpc_clients: dict[str | None, oslo_messaging.RPCClient] = {}
        self._rpc_transport: oslo_messaging.Transport | None = None

    def start(self) -> None:
        """Start the evaluation loop.  Blocks until stopped."""
        policies = load_policies(self._conf.engine.policies_file)
        interval = self._conf.engine.evaluation_interval
        dry_run = self._conf.engine.dry_run
        aggregates = self._resolve_aggregates()

        if not aggregates:
            raise RuntimeError(
                "Engine has no aggregates to manage. Set [engine] aggregates "
                "or [engine] include_unassigned_hosts in kronos.conf.",
            )

        if not dry_run:
            self._init_messaging(aggregates)

        LOG.info(
            "Starting engine loop: interval=%ds, dry_run=%s, policies=%d, aggregates=%s",
            interval,
            dry_run,
            len(policies.policies),
            [a if a is not None else "<unassigned>" for a in aggregates],
        )

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        while self._running:
            report = self._run_cycle(policies, aggregates, dry_run)
            self._log_report(report)

            if self._running:
                time.sleep(interval)

        LOG.info("Engine loop stopped after %d cycles.", self._cycle_count)

    def stop(self) -> None:
        """Signal the loop to stop after the current cycle."""
        self._running = False

    def _resolve_aggregates(self) -> list[str | None]:
        """Build the list of aggregate names (and optional None for unassigned)."""
        result: list[str | None] = list(self._conf.engine.aggregates)
        if self._conf.engine.include_unassigned_hosts:
            result.append(None)
        return result

    def _init_messaging(self, aggregates: list[str | None]) -> None:
        """Create one RPC client per aggregate (or for the unassigned pool)."""
        self._rpc_transport = get_rpc_transport(self._conf)
        for aggregate in aggregates:
            self._rpc_clients[aggregate] = get_rpc_client(
                self._rpc_transport, aggregate,
            )
            LOG.info(
                "RPC client ready for topic '%s'",
                migrations_topic(aggregate),
            )

    def _run_cycle(
        self,
        policies: PoliciesConfig,
        aggregates: list[str | None],
        dry_run: bool,
    ) -> CycleReport:
        """Evaluate every aggregate in the engine's scope once."""
        self._cycle_count += 1
        started_at = datetime.now(tz=UTC)

        self._constraints.invalidate_cache()

        report = CycleReport(
            cycle_number=self._cycle_count,
            started_at=started_at,
            completed_at=started_at,
            dry_run=dry_run,
        )

        enabled_policies = [p for p in policies.policies if p.enabled]

        for aggregate in aggregates:
            try:
                result = self._evaluate_aggregate(
                    aggregate, enabled_policies, dry_run,
                )
                report.aggregate_results.append(result)
            except Exception as exc:
                name = aggregate if aggregate is not None else "<unassigned>"
                error_msg = f"Aggregate '{name}': {exc}"
                LOG.error("Evaluation error: %s", error_msg)
                report.errors.append(error_msg)

        report.completed_at = datetime.now(tz=UTC)
        return report

    def _evaluate_aggregate(
        self,
        aggregate: str | None,
        policies: list[PolicyConfig],
        dry_run: bool,
    ) -> AggregateResult:
        """Run all policies against one aggregate and plan migrations."""
        name = aggregate if aggregate is not None else "<unassigned>"
        hosts = self._nova.get_hosts_in_aggregate(aggregate)

        result = AggregateResult(aggregate=name)

        if not hosts:
            LOG.info("Aggregate '%s' has no hosts, skipping.", name)
            return result

        # Score every policy in turn
        policy_results: list[PolicyResult] = [
            self._scorer.evaluate(policy, hosts) for policy in policies
        ]
        result.policy_results = policy_results

        # Combined imbalance signal — weighted sum across all non-skipped policies
        combined_imbalance = 0.0
        any_detected = False
        for policy, pr in zip(policies, policy_results, strict=True):
            if pr.skipped:
                continue
            combined_imbalance += policy.weight * pr.imbalance
            if pr.imbalance_detected:
                any_detected = True
        result.combined_imbalance = combined_imbalance
        result.imbalance_detected = any_detected

        if not any_detected:
            return result

        # Cooldown check
        if not dry_run and self._cooldown.is_aggregate_cooling(name):
            LOG.info(
                "Aggregate '%s': imbalance detected but cooldown active, skipping planning.",
                name,
            )
            return result

        # Collect per-policy VM profiles
        active_policies = [
            p for p, pr in zip(policies, policy_results, strict=True)
            if not pr.skipped
        ]
        host_scores_by_policy = {
            p.name: {hs.host: hs.raw_score for hs in pr.host_scores}
            for p, pr in zip(policies, policy_results, strict=True)
            if not pr.skipped
        }
        vm_profiles = self._profiler.collect(
            policies=active_policies,
            hosts=hosts,
            host_scores_by_policy=host_scores_by_policy,
        )
        result.vm_profiles = vm_profiles

        if not vm_profiles:
            return result

        # Plan with combined scoring
        plan = self._planner.plan(
            aggregate=name,
            policies=active_policies,
            policy_results=[
                pr for p, pr in zip(policies, policy_results, strict=True)
                if not pr.skipped
            ],
            vm_profiles=vm_profiles,
        )
        result.migration_plan = plan

        if not dry_run and plan.steps:
            self._emit_plan(plan, aggregate)
            self._cooldown.record_plan_emission(
                name,
                [step.instance_uuid for step in plan.steps],
            )

        return result

    def _emit_plan(
        self,
        plan: MigrationPlan,
        aggregate: str | None,
    ) -> None:
        """RPC cast migration tasks for the aggregate's executor."""
        client = self._rpc_clients.get(aggregate)
        if client is None:
            LOG.warning(
                "No RPC client for aggregate '%s'; skipping plan emission.",
                plan.aggregate,
            )
            return

        plan_id = str(uuid.uuid4())
        stagger = self._conf.executor.stagger_seconds
        base_time = time.time()

        for i, step in enumerate(plan.steps):
            task = MigrationTask.from_step(
                step=step,
                plan_id=plan_id,
                aggregate=plan.aggregate,
                policy_names=plan.policy_names,
                not_before=base_time + (i * stagger),
                max_retries=self._conf.executor.max_retries,
            )
            client.cast({}, "execute_migration", task=task_to_dict(task))
            LOG.info(
                "Cast migration task %s: %s (%s) %s -> %s "
                "(plan=%s, not_before=+%ds)",
                task.task_id[:8],
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                plan_id[:8],
                i * stagger,
            )

    def _log_report(self, report: CycleReport) -> None:
        duration = (report.completed_at - report.started_at).total_seconds()

        LOG.info(
            "Cycle #%d completed in %.1fs: %d aggregates, %d errors",
            report.cycle_number,
            duration,
            len(report.aggregate_results),
            len(report.errors),
        )

        for ar in report.aggregate_results:
            self._log_aggregate_result(ar)

    @staticmethod
    def _log_aggregate_result(ar: AggregateResult) -> None:
        if ar.imbalance_detected:
            LOG.info(
                "  [IMBALANCE] %s: combined=%.3f",
                ar.aggregate,
                ar.combined_imbalance,
            )
        else:
            LOG.info(
                "  [OK] %s: combined=%.3f (within thresholds)",
                ar.aggregate,
                ar.combined_imbalance,
            )

        for pr in ar.policy_results:
            if pr.skipped:
                LOG.info("    [SKIP] %s: %s", pr.policy_name, pr.skip_reason)
                continue
            marker = "⚠" if pr.imbalance_detected else " "
            LOG.info(
                "    %s %s: imbalance=%.3f",
                marker,
                pr.policy_name,
                pr.imbalance,
            )
            for hs in pr.host_scores:
                LOG.debug(
                    "        %s: raw=%.3f normalized=%.3f",
                    hs.host,
                    hs.raw_score,
                    hs.normalized_score,
                )

        EngineLoop._log_migration_plan(ar.migration_plan)

    @staticmethod
    def _log_migration_plan(plan: MigrationPlan | None) -> None:
        if plan is None or not plan.steps:
            return

        LOG.info(
            "    Migration plan: %d steps, combined %.3f -> %.3f (projected)",
            plan.migration_count,
            plan.initial_imbalance,
            plan.projected_imbalance,
        )
        for i, step in enumerate(plan.steps, 1):
            LOG.info(
                "    [%d] %s (%s): %s -> %s (improvement=%.3f)",
                i,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                step.improvement,
            )

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down gracefully...", sig_name)
        self._running = False

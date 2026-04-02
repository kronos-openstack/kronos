"""Main engine control loop."""

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
    emit_notification,
    get_notifier,
    get_transport,
    migrations_topic,
    task_to_dict,
)
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.cooldown import CooldownTracker
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import (
    CycleReport,
    MigrationPlan,
    MigrationTask,
    PolicyResult,
)
from kronos.policies.loader import load_policies
from kronos.policies.models import PoliciesConfig, PolicyConfig

LOG = logging.getLogger(__name__)


class EngineLoop:
    """Periodic evaluation loop.

    Evaluates all enabled policies on each cycle.  When imbalance is
    detected, collects VM profiles and runs the planner to produce a
    migration plan.  In M2 the plan is logged (dry-run only).
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        self._conf = conf
        self._prometheus = PrometheusClient(conf)
        self._nova = NovaClient(conf)
        self._scorer = PolicyScorer(self._prometheus, self._nova)
        self._profiler = VmProfiler(self._prometheus, self._nova)
        self._constraints = ConstraintChecker(self._nova)
        self._planner = Planner(self._constraints)
        self._cooldown = CooldownTracker(
            instance_cooldown_seconds=float(conf.engine.instance_cooldown),
        )
        self._running = False
        self._cycle_count = 0

        # Messaging — only initialised when dry_run=False
        self._notifiers: dict[str, oslo_messaging.Notifier] = {}
        self._transport: oslo_messaging.Transport | None = None

    def start(self) -> None:
        """Start the evaluation loop. Blocks until stopped."""
        policies = load_policies(self._conf.engine.policies_file)
        interval = self._conf.engine.evaluation_interval
        dry_run = self._conf.engine.dry_run

        if not dry_run:
            self._init_messaging(policies)

        LOG.info(
            "Starting engine loop: interval=%ds, dry_run=%s, policies=%d",
            interval,
            dry_run,
            len(policies.policies),
        )

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        while self._running:
            report = self._run_cycle(policies, dry_run)
            self._log_report(report)

            if self._running:
                time.sleep(interval)

        LOG.info("Engine loop stopped after %d cycles.", self._cycle_count)

    def stop(self) -> None:
        """Signal the loop to stop after the current cycle."""
        self._running = False

    def _run_cycle(self, policies: PoliciesConfig, dry_run: bool) -> CycleReport:
        """Evaluate all enabled policies once."""
        self._cycle_count += 1
        started_at = datetime.now(tz=UTC)

        # Invalidate constraint caches each cycle
        self._constraints.invalidate_cache()

        report = CycleReport(
            cycle_number=self._cycle_count,
            started_at=started_at,
            completed_at=started_at,
            dry_run=dry_run,
        )

        for policy in policies.policies:
            if not policy.enabled:
                continue

            try:
                result = self._evaluate_policy(policy, dry_run)
                report.policy_results.append(result)
            except Exception as exc:
                error_msg = f"Policy '{policy.name}': {exc}"
                LOG.error("Evaluation error: %s", error_msg)
                report.errors.append(error_msg)

        report.completed_at = datetime.now(tz=UTC)
        return report

    def _init_messaging(self, policies: PoliciesConfig) -> None:
        """Initialise oslo.messaging transport and per-aggregate notifiers.

        :raises: Exception if transport creation fails — the engine must
            not start in non-dry-run mode without a working transport.
        """
        self._transport = get_transport(self._conf)

        aggregates = {p.aggregate for p in policies.policies if p.enabled}
        for aggregate in aggregates:
            topic = migrations_topic(aggregate)
            self._notifiers[aggregate] = get_notifier(
                self._transport, topic, publisher_id=f"kronos-engine-{aggregate}",
            )
            LOG.info("Messaging notifier ready for topic '%s'", topic)

    def _evaluate_policy(self, policy: PolicyConfig, dry_run: bool) -> PolicyResult:
        """Score a policy, and if imbalanced, profile VMs and plan migrations."""
        result = self._scorer.evaluate(policy)

        if result.skipped or not result.imbalance_detected:
            return result

        # Skip planning if the policy is still cooling down
        if not dry_run and self._cooldown.is_policy_cooling(policy.name, policy.cooldown):
            LOG.info(
                "Policy '%s': imbalance detected but cooldown active, skipping planning.",
                policy.name,
            )
            return result

        # Imbalance detected — collect VM profiles and plan
        raw_scores = {hs.host: hs.raw_score for hs in result.host_scores}
        aggregate_hosts = list(raw_scores.keys())

        vm_profiles = self._profiler.collect(
            policy=policy,
            aggregate_hosts=aggregate_hosts,
            host_scores=raw_scores,
        )
        result.vm_profiles = vm_profiles

        if vm_profiles:
            plan = self._planner.plan(
                policy=policy,
                host_scores=result.host_scores,
                vm_profiles=vm_profiles,
            )
            result.migration_plan = plan

            if not dry_run and plan.steps:
                self._emit_plan(plan, policy)
                self._cooldown.record_plan_emission(
                    policy.name,
                    [step.instance_uuid for step in plan.steps],
                )

        return result

    def _emit_plan(self, plan: MigrationPlan, policy: PolicyConfig) -> None:
        """Publish migration tasks to the aggregate's messaging topic."""
        notifier = self._notifiers.get(policy.aggregate)
        if notifier is None:
            LOG.warning(
                "No notifier for aggregate '%s'; skipping plan emission.",
                policy.aggregate,
            )
            return

        plan_id = str(uuid.uuid4())
        stagger = self._conf.executor.stagger_seconds
        base_time = time.time()

        for i, step in enumerate(plan.steps):
            task = MigrationTask.from_step(
                step=step,
                plan_id=plan_id,
                policy_name=policy.name,
                aggregate=policy.aggregate,
                not_before=base_time + (i * stagger),
                max_retries=self._conf.executor.max_retries,
            )
            emit_notification(notifier, "migration.requested", task_to_dict(task))
            LOG.info(
                "Emitted migration task %s: %s (%s) %s -> %s "
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
        """Log cycle results."""
        duration = (report.completed_at - report.started_at).total_seconds()

        LOG.info(
            "Cycle #%d completed in %.1fs: %d policies evaluated, %d errors",
            report.cycle_number,
            duration,
            len(report.policy_results),
            len(report.errors),
        )

        for result in report.policy_results:
            if result.skipped:
                LOG.info(
                    "  [SKIP] %s: %s",
                    result.policy_name,
                    result.skip_reason,
                )
            elif result.imbalance_detected:
                LOG.info(
                    "  [IMBALANCE] %s: imbalance=%.3f",
                    result.policy_name,
                    result.imbalance,
                )
                for hs in result.host_scores:
                    LOG.info(
                        "    %s: raw=%.3f normalized=%.3f",
                        hs.host,
                        hs.raw_score,
                        hs.normalized_score,
                    )
                self._log_migration_plan(result.migration_plan)
            else:
                LOG.info(
                    "  [OK] %s: imbalance=%.3f (within threshold)",
                    result.policy_name,
                    result.imbalance,
                )
                for hs in result.host_scores:
                    LOG.debug(
                        "    %s: raw=%.3f normalized=%.3f",
                        hs.host,
                        hs.raw_score,
                        hs.normalized_score,
                    )

    @staticmethod
    def _log_migration_plan(plan: MigrationPlan | None) -> None:
        """Log a migration plan (dry-run output)."""
        if plan is None or not plan.steps:
            LOG.info("    No migrations proposed.")
            return

        LOG.info(
            "    Migration plan: %d steps, imbalance %.3f -> %.3f (projected)",
            plan.migration_count,
            plan.initial_imbalance,
            plan.projected_imbalance,
        )
        for i, step in enumerate(plan.steps, 1):
            LOG.info(
                "    [%d] %s (%s): %s -> %s (weight=%.3f, improvement=%.3f)",
                i,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                step.weight,
                step.improvement,
            )

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down gracefully...", sig_name)
        self._running = False

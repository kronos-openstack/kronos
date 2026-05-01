"""Main engine control loop.

The engine operates on a fixed set of aggregates defined at startup via
``[engine] aggregates`` and ``[engine] include_unassigned_hosts``.  Each
aggregate is evaluated independently every cycle:

1. resolve aggregate -> host list via Nova
2. run every enabled policy against that host list (scorer)
3. collect VM profiles for all policies in one pass (profiler)
4. combined-scoring planner produces at most one migration plan
5. cast migration tasks over RPC to the executor (unless dry_run)
"""

from __future__ import annotations

import signal
import threading
import time
import uuid
from datetime import UTC, datetime
from types import FrameType

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import ComputeService, NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.messaging import (
    get_notification_listener,
    get_notification_transport,
    get_rpc_client,
    get_rpc_transport,
    migrations_topic,
    results_topic,
    task_to_dict,
)
from kronos.engine._sim import combined_imbalance, group_vms_by_host
from kronos.engine.affinity_enforcer import AffinityEnforcer
from kronos.engine.constraints import ConstraintChecker
from kronos.engine.cooldown import CooldownTracker
from kronos.engine.evacuator import Evacuator
from kronos.engine.planner import Planner
from kronos.engine.profiler import VmProfiler
from kronos.engine.result_listener import MigrationResultEndpoint
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import (
    AggregateResult,
    CycleReport,
    MigrationPlan,
    MigrationTask,
    PolicyResult,
    VmProfile,
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
        self._enforcer = AffinityEnforcer(
            self._constraints,
            enforce_hard=conf.engine.enforce_hard_affinity,
            enforce_soft=conf.engine.enforce_soft_affinity,
        )
        self._evacuator = Evacuator(
            self._constraints,
            enabled=conf.engine.evacuate_disabled_hosts,
        )
        self._cooldown = CooldownTracker(
            aggregate_cooldown_seconds=float(conf.engine.cooldown),
            instance_cooldown_seconds=float(conf.engine.instance_cooldown),
        )
        self._running = False
        self._cycle_count = 0

        # Wakeup event used as the inter-cycle sleep.  CPython retries
        # `time.sleep()` on EINTR (PEP 475), so a SIGTERM during the
        # sleep would otherwise have to wait the full interval before
        # the loop notices.  Setting this from the signal handler
        # unblocks the wait immediately.
        self._wakeup = threading.Event()

        # Messaging - only initialised when dry_run=False.
        # Keys: aggregate name, or None for the unassigned-hosts pool.
        self._rpc_clients: dict[str | None, oslo_messaging.RPCClient] = {}
        self._rpc_transport: oslo_messaging.Transport | None = None

        # Result listener state (one listener per aggregate).
        self._notification_transport: oslo_messaging.Transport | None = None
        self._result_listeners: list[oslo_messaging.MessageHandlingServer] = []

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
                # Interruptible inter-cycle sleep.  The signal handler
                # sets `_wakeup` so SIGTERM/SIGINT exits within
                # milliseconds instead of waiting the full interval.
                self._wakeup.wait(timeout=interval)
                self._wakeup.clear()

        self._shutdown_messaging()
        LOG.info("Engine loop stopped after %d cycles.", self._cycle_count)

    def stop(self) -> None:
        """Signal the loop to stop after the current cycle."""
        self._running = False
        self._wakeup.set()

    def _shutdown_messaging(self) -> None:
        """Stop and drain the result listeners.

        ``MessageHandlingServer.wait()`` has no timeout in oslo.messaging;
        if a transport thread doesn't unwind we drain in a daemon
        thread and continue after a deadline rather than blocking the
        process forever.
        """
        if not self._result_listeners:
            return

        listeners = list(self._result_listeners)
        self._result_listeners.clear()

        def _drain() -> None:
            for listener in listeners:
                try:
                    listener.stop()
                    listener.wait()
                except Exception as exc:
                    LOG.warning(
                        "Error while stopping result listener: %s", exc,
                    )

        drainer = threading.Thread(
            target=_drain, name="engine-shutdown", daemon=True,
        )
        drainer.start()
        drainer.join(timeout=10.0)
        if drainer.is_alive():
            LOG.warning(
                "Result listeners did not drain within 10s; "
                "exiting anyway.",
            )

    def _drop_vms_on_unreachable_hosts(
        self,
        vm_profiles: dict[str, VmProfile],
        aggregate_hosts: list[str],
        services: dict[str, ComputeService],
        aggregate_name: str,
    ) -> dict[str, VmProfile]:
        """Drop VMs whose source host's nova-compute service is down.

        Live migration can't move a VM off a hypervisor whose
        nova-compute service is offline (``state=down``) or whose
        service is missing entirely from the cluster's view.  These
        VMs need cold migration / evacuate, which Kronos does not
        perform.  Excluding them from the candidate set stops the
        planner from generating doomed-to-fail tasks.

        Hosts whose service is ``status=disabled`` but ``state=up`` are
        *not* dropped here - the evacuator may still want to drain them
        when ``[engine] evacuate_disabled_hosts`` is set.
        """
        scope = set(aggregate_hosts)
        kept: dict[str, VmProfile] = {}
        dropped: list[str] = []
        for instance_uuid, profile in vm_profiles.items():
            if profile.host not in scope:
                # Not in this aggregate - shouldn't happen, but be
                # defensive about the profile/hosts mismatch.
                kept[instance_uuid] = profile
                continue
            svc = services.get(profile.host)
            if svc is None or not svc.is_up:
                dropped.append(instance_uuid)
                continue
            kept[instance_uuid] = profile

        if dropped:
            LOG.info(
                "Aggregate '%s': dropped %d VM(s) on hosts whose "
                "nova-compute service is down/missing: %s",
                aggregate_name,
                len(dropped),
                dropped,
            )
        return kept

    def _filter_unavailable_vms(
        self,
        vm_profiles: dict[str, VmProfile],
        aggregate: str,
    ) -> dict[str, VmProfile]:
        """Drop VMs currently under quarantine or instance cooldown.

        Both states mean "do not plan a migration for this VM right now":

        - Quarantine: the VM's migration definitively failed recently
          (final attempt hit PreFlightError / MigrationFailed /
          MigrationTimeout); skip until the quarantine expires.
        - Instance cooldown: the VM was included in a recently emitted
          plan; skip until ``instance_cooldown`` elapses to prevent the
          VM from bouncing between hosts.
        """
        kept: dict[str, VmProfile] = {}
        quarantined: list[str] = []
        cooling: list[str] = []
        for instance_uuid, profile in vm_profiles.items():
            if self._cooldown.is_instance_quarantined(instance_uuid):
                quarantined.append(instance_uuid)
                continue
            if self._cooldown.is_instance_cooling(instance_uuid):
                cooling.append(instance_uuid)
                continue
            kept[instance_uuid] = profile

        if quarantined or cooling:
            LOG.debug(
                "Aggregate '%s': excluded %d quarantined and %d cooling VMs "
                "from planning (quarantined=%s, cooling=%s)",
                aggregate,
                len(quarantined),
                len(cooling),
                quarantined,
                cooling,
            )
        return kept

    def _resolve_aggregates(self) -> list[str | None]:
        """Build the list of aggregate names (and optional None for unassigned)."""
        result: list[str | None] = list(self._conf.engine.aggregates)
        if self._conf.engine.include_unassigned_hosts:
            result.append(None)
        return result

    def _init_messaging(self, aggregates: list[str | None]) -> None:
        """Wire RPC clients (outbound) and result listeners (inbound)."""
        self._rpc_transport = get_rpc_transport(self._conf)
        for aggregate in aggregates:
            self._rpc_clients[aggregate] = get_rpc_client(
                self._rpc_transport, aggregate,
            )
            LOG.info(
                "RPC client ready for topic '%s'",
                migrations_topic(aggregate),
            )

        self._notification_transport = get_notification_transport(self._conf)
        endpoint = MigrationResultEndpoint(
            cooldown=self._cooldown,
            max_retries=self._conf.executor.max_retries,
            quarantine_seconds=self._conf.engine.instance_quarantine_seconds,
        )
        for aggregate in aggregates:
            topic = results_topic(aggregate)
            listener = get_notification_listener(
                self._notification_transport, topic, [endpoint],
            )
            listener.start()
            self._result_listeners.append(listener)
            LOG.info("Result listener ready on topic '%s'", topic)

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

        # One per-cycle nova-compute service map shared by the
        # constraint checker (gates destinations) and the evacuator
        # (selects disabled-host candidates).  Cluster-wide; each
        # aggregate intersects with its own host list.
        services = self._fetch_services()
        self._constraints.set_services(services)

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
                    aggregate, enabled_policies, dry_run, services,
                )
                report.aggregate_results.append(result)
            except Exception as exc:
                name = aggregate if aggregate is not None else "<unassigned>"
                error_msg = f"Aggregate '{name}': {exc}"
                LOG.error("Evaluation error: %s", error_msg)
                report.errors.append(error_msg)

        report.completed_at = datetime.now(tz=UTC)
        return report

    def _fetch_services(self) -> dict[str, ComputeService]:
        """Fetch nova-compute services once per cycle.

        On Nova API failure we log and fall back to an empty map, which
        the constraint checker treats as "no host is available" - the
        engine effectively pauses migrations for the cycle.  That is
        the right behaviour: we don't know who is up.
        """
        try:
            services = self._nova.list_compute_services()
        except Exception:
            LOG.warning(
                "Failed to fetch nova-compute services; planning paused "
                "this cycle (no destinations considered available).",
                exc_info=True,
            )
            return {}
        return {svc.host: svc for svc in services}

    def _evaluate_aggregate(
        self,
        aggregate: str | None,
        policies: list[PolicyConfig],
        dry_run: bool,
        services: dict[str, ComputeService],
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

        # Combined imbalance signal - weighted sum across all non-skipped policies
        weighted_imbalance = 0.0
        any_detected = False
        for policy, pr in zip(policies, policy_results, strict=True):
            if pr.skipped:
                continue
            weighted_imbalance += policy.weight * pr.imbalance
            if pr.imbalance_detected:
                any_detected = True
        result.combined_imbalance = weighted_imbalance
        result.imbalance_detected = any_detected

        # Profiling + planning run whenever imbalance is detected, the
        # affinity enforcer is enabled, or the evacuator is enabled
        # (any of them can produce moves even without imbalance signal).
        if (
            not any_detected
            and not self._enforcer.enabled
            and not self._evacuator.enabled
        ):
            return result

        # Cooldown check applies to any migration emission.  Enforced
        # in dry-run too so the simulated cycle rhythm matches what
        # production would actually do.
        if self._cooldown.is_aggregate_cooling(name):
            LOG.info(
                "Aggregate '%s': cooldown active, skipping planning.",
                name,
            )
            return result

        active_policies = [
            p for p, pr in zip(policies, policy_results, strict=True)
            if not pr.skipped
        ]
        active_results = [
            pr for pr in policy_results if not pr.skipped
        ]
        if not active_policies:
            return result

        host_scores_by_policy = {
            p.name: {hs.host: hs.raw_score for hs in pr.host_scores}
            for p, pr in zip(active_policies, active_results, strict=True)
        }
        vm_profiles = self._profiler.collect(
            policies=active_policies,
            hosts=hosts,
            host_scores_by_policy=host_scores_by_policy,
        )
        vm_profiles = self._drop_vms_on_unreachable_hosts(
            vm_profiles, hosts, services, name,
        )
        vm_profiles = self._filter_unavailable_vms(vm_profiles, name)
        result.vm_profiles = vm_profiles

        if not vm_profiles:
            return result

        budget = max(p.max_migrations_per_cycle for p in active_policies)

        scores = {
            p.name: {hs.host: hs.raw_score for hs in pr.host_scores}
            for p, pr in zip(active_policies, active_results, strict=True)
        }
        vms_by_host = group_vms_by_host(vm_profiles)

        plan = MigrationPlan(
            aggregate=name,
            policy_names=[p.name for p in active_policies],
            initial_imbalance=combined_imbalance(scores, active_policies),
        )

        # --- Evacuation pass (drain admin-disabled hosts) ---
        # Runs first so the affinity enforcer and the imbalance planner
        # see the post-evacuation simulated state.
        evac_plan, scores, vms_by_host, remaining = self._evacuator.evacuate(
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

        # --- Affinity enforcement pass (repair existing violations) ---
        enforce_plan, scores, vms_by_host, remaining = self._enforcer.enforce(
            aggregate=name,
            policies=active_policies,
            policy_results=active_results,
            vm_profiles=vm_profiles,
            budget=remaining,
            scores=scores,
            vms_by_host=vms_by_host,
        )
        plan.steps.extend(enforce_plan.steps)

        # --- Imbalance pass (reduce PromQL-driven imbalance) ---
        # Only runs if the scorer flagged imbalance and budget remains.
        # The earlier passes may have left some imbalance behind; the
        # planner tackles it within ``remaining`` moves.
        if any_detected:
            plan = self._planner.plan(
                aggregate=name,
                policies=active_policies,
                policy_results=active_results,
                vm_profiles=vm_profiles,
                scores=scores,
                vms_by_host=vms_by_host,
                budget=remaining,
                plan=plan,
            )

        plan.projected_imbalance = combined_imbalance(scores, active_policies)
        result.migration_plan = plan

        if plan.steps:
            if not dry_run:
                self._emit_plan(plan, aggregate)
            # Record cooldown unconditionally - keeping dry-run's rhythm
            # aligned with production means the simulated log shows
            # exactly the skipped cycles an operator would see live.
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

        total = len(plan.steps)
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
                "Dispatched move %d/%d (%s): %s (%s) %s -> %s [task %s, plan %s, starts in %ds]",
                i + 1,
                total,
                step.phase.value,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                task.task_id[:8],
                plan_id[:8],
                i * stagger,
            )

    def _log_report(self, report: CycleReport) -> None:
        duration = (report.completed_at - report.started_at).total_seconds()

        LOG.info(
            "Cycle %d finished in %.1fs (%d aggregates, %d errors)",
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
                "Aggregate '%s': combined imbalance %.3f, above threshold",
                ar.aggregate,
                ar.combined_imbalance,
            )
        else:
            LOG.info(
                "Aggregate '%s': combined imbalance %.3f, within thresholds",
                ar.aggregate,
                ar.combined_imbalance,
            )

        for pr in ar.policy_results:
            if pr.skipped:
                LOG.info(
                    "policy %s skipped (%s)",
                    pr.policy_name, pr.skip_reason,
                )
                continue
            suffix = " (threshold exceeded)" if pr.imbalance_detected else ""
            LOG.info(
                "policy %s imbalance %.3f%s",
                pr.policy_name, pr.imbalance, suffix,
            )
            for hs in pr.host_scores:
                LOG.debug(
                    "host %s raw=%.3f normalized=%.3f",
                    hs.host, hs.raw_score, hs.normalized_score,
                )

        EngineLoop._log_migration_plan(ar.migration_plan)

    @staticmethod
    def _log_migration_plan(plan: MigrationPlan | None) -> None:
        if plan is None or not plan.steps:
            return

        LOG.info(
            "Plan: %d moves, combined imbalance %.3f -> %.3f (projected)",
            plan.migration_count,
            plan.initial_imbalance,
            plan.projected_imbalance,
        )
        for i, step in enumerate(plan.steps, 1):
            LOG.info(
                "%d. %s %s (%s) %s -> %s, gain %.3f",
                i,
                step.phase.value,
                step.instance_name,
                step.instance_uuid[:8],
                step.from_host,
                step.to_host,
                step.improvement,
            )

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        """Signal handler: flip the flag and unblock the inter-cycle wait.

        Signal handlers run in the main thread between bytecodes; do
        only async-signal-safe work here.  The actual shutdown
        (messaging drain, listener join) runs from `start()` once the
        loop sees `_running == False`.
        """
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down gracefully...", sig_name)
        self._running = False
        self._wakeup.set()

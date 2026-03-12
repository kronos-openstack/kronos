"""Main engine control loop."""

from __future__ import annotations

import signal
import time
from datetime import UTC, datetime
from types import FrameType

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import CycleReport
from kronos.policies.loader import load_policies
from kronos.policies.models import PoliciesConfig

LOG = logging.getLogger(__name__)


class EngineLoop:
    """Periodic evaluation loop.

    Evaluates all enabled policies on each cycle and logs results.
    M1: dry-run only — no migrations, no queue publishing.
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        self._conf = conf
        self._prometheus = PrometheusClient(conf)
        self._nova = NovaClient(conf)
        self._scorer = PolicyScorer(self._prometheus, self._nova)
        self._running = False
        self._cycle_count = 0

    def start(self) -> None:
        """Start the evaluation loop. Blocks until stopped."""
        policies = load_policies(self._conf.engine.policies_file)
        interval = self._conf.engine.evaluation_interval
        dry_run = self._conf.engine.dry_run

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
                result = self._scorer.evaluate(policy)
                report.policy_results.append(result)
            except Exception as exc:
                error_msg = f"Policy '{policy.name}': {exc}"
                LOG.error("Evaluation error: %s", error_msg)
                report.errors.append(error_msg)

        report.completed_at = datetime.now(tz=UTC)
        return report

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
                    "  [IMBALANCE] %s: imbalance=%.3f (threshold=exceeded)",
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
            else:
                LOG.info(
                    "  [OK] %s: imbalance=%.3f (within threshold)",
                    result.policy_name,
                    result.imbalance,
                )
                # Optionally log host scores for non-imbalanced policies as well
                for hs in result.host_scores:
                    LOG.debug(
                        "    %s: raw=%.3f normalized=%.3f",
                        hs.host,
                        hs.raw_score,
                        hs.normalized_score,
                    )

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down gracefully...", sig_name)
        self._running = False

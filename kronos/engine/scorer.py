"""Policy evaluation: PromQL queries → per-host scores."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from oslo_log import log as logging

from kronos.clients.prometheus import PrometheusClient
from kronos.engine.types import HostScore, PolicyResult
from kronos.policies.models import PolicyConfig

LOG = logging.getLogger(__name__)


class PolicyScorer:
    """Evaluates a single policy's PromQL imbalance query against a set of hosts.

    The engine resolves the aggregate → host list and hands it to the
    scorer.  The scorer runs the PromQL query, matches results to the
    host list, normalises the values, enforces the [0, 1] contract, and
    detects imbalance.
    """

    def __init__(self, prometheus: PrometheusClient) -> None:
        self._prometheus = prometheus

    def evaluate(
        self,
        policy: PolicyConfig,
        hosts: list[str],
    ) -> PolicyResult:
        """Evaluate a policy against a concrete host list.

        :param policy: The policy to evaluate.
        :param hosts: The host set this evaluation is scoped to.
        :returns: PolicyResult with host scores and imbalance detection.
        """
        start = time.monotonic()
        now = datetime.now(tz=UTC)

        if not hosts:
            return self._skipped(
                policy, now, start,
                "No hosts in scope.",
            )

        expected_hosts = set(hosts)
        result = self._prometheus.instant_query(
            query=policy.imbalance_query,
            label_key=policy.host_label,
            expected_labels=expected_hosts,
        )

        if not result.is_trustworthy:
            return self._skipped(
                policy, now, start,
                f"Untrustworthy data (health={result.health.value}): "
                f"{'; '.join(result.warnings)}",
            )

        filtered = {
            host: score
            for host, score in result.series.items()
            if host in expected_hosts
        }

        if not filtered:
            return self._skipped(
                policy, now, start,
                "No matching host data from Prometheus.",
            )

        # Enforce the [0, 1] contract - the policy's imbalance_query must
        # return utilization ratios, not absolute values.
        out_of_range = {
            h: v for h, v in filtered.items() if v < 0.0 or v > 1.0
        }
        if out_of_range:
            LOG.error(
                "Policy '%s': imbalance_query returned out-of-range values: %s. "
                "Policy imbalance_query must yield values in [0, 1].",
                policy.name,
                out_of_range,
            )
            return self._skipped(
                policy, now, start,
                f"imbalance_query returned out-of-range values: {out_of_range}",
            )

        host_scores = self._normalize_scores(filtered)
        imbalance = self._compute_imbalance(host_scores)
        imbalance_detected = imbalance > policy.threshold

        duration_ms = (time.monotonic() - start) * 1000

        LOG.info(
            "Policy '%s': imbalance=%.3f threshold=%.3f detected=%s (%.1fms)",
            policy.name,
            imbalance,
            policy.threshold,
            imbalance_detected,
            duration_ms,
        )

        return PolicyResult(
            policy_name=policy.name,
            mode=policy.mode,
            host_scores=host_scores,
            imbalance=imbalance,
            imbalance_detected=imbalance_detected,
            timestamp=now,
            evaluation_duration_ms=duration_ms,
        )

    @staticmethod
    def _skipped(
        policy: PolicyConfig,
        now: datetime,
        start: float,
        reason: str,
    ) -> PolicyResult:
        return PolicyResult(
            policy_name=policy.name,
            mode=policy.mode,
            host_scores=[],
            imbalance=0.0,
            imbalance_detected=False,
            timestamp=now,
            evaluation_duration_ms=(time.monotonic() - start) * 1000,
            skipped=True,
            skip_reason=reason,
        )

    @staticmethod
    def _normalize_scores(raw: dict[str, float]) -> list[HostScore]:
        """Normalize raw scores to 0.0-1.0 using min-max."""
        min_val = min(raw.values())
        max_val = max(raw.values())
        spread = max_val - min_val or 0.5

        return [
            HostScore(
                host=host,
                raw_score=score,
                normalized_score=(score - min_val) / spread,
            )
            for host, score in sorted(raw.items(), key=lambda x: x[1], reverse=True)
        ]

    @staticmethod
    def _compute_imbalance(scores: list[HostScore]) -> float:
        """Compute imbalance as (max - min) of raw scores."""
        if len(scores) < 2:
            return 0.0
        raw_values = [s.raw_score for s in scores]
        return max(raw_values) - min(raw_values)

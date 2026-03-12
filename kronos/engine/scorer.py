"""Policy evaluation: PromQL queries → per-host scores."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.exceptions import PolicyEvaluationError
from kronos.engine.types import HostScore, PolicyResult
from kronos.policies.models import PolicyConfig

LOG = logging.getLogger(__name__)


class PolicyScorer:
    """Evaluates a single policy's PromQL queries and produces host scores.

    Steps:
      1. Get hosts in the policy's aggregate from Nova
      2. Query Prometheus with imbalance_query
      3. Match results to Nova hosts
      4. Normalize scores within the aggregate
      5. Detect imbalance
    """

    def __init__(
        self,
        prometheus: PrometheusClient,
        nova: NovaClient,
    ) -> None:
        self._prometheus = prometheus
        self._nova = nova

    def evaluate(self, policy: PolicyConfig) -> PolicyResult:
        """Evaluate a policy and return scored hosts.

        :param policy: The policy to evaluate.
        :returns: PolicyResult with host scores and imbalance detection.
        :raises PolicyEvaluationError: If evaluation fails.
        """
        start = time.monotonic()
        now = datetime.now(tz=UTC)

        try:
            aggregate_hosts = self._nova.get_aggregate_hosts(policy.aggregate)
        except Exception as exc:
            raise PolicyEvaluationError(
                policy_name=policy.name,
                reason=f"Failed to get aggregate hosts: {exc}",
            ) from exc

        if not aggregate_hosts:
            return PolicyResult(
                policy_name=policy.name,
                mode=policy.mode,
                aggregate=policy.aggregate,
                host_scores=[],
                imbalance=0.0,
                imbalance_detected=False,
                timestamp=now,
                evaluation_duration_ms=(time.monotonic() - start) * 1000,
                skipped=True,
                skip_reason=f"Aggregate '{policy.aggregate}' has no hosts.",
            )

        expected_hosts = set(aggregate_hosts)
        result = self._prometheus.instant_query(
            query=policy.imbalance_query,
            label_key=policy.host_label,
            expected_labels=expected_hosts,
        )

        if not result.is_trustworthy:
            return PolicyResult(
                policy_name=policy.name,
                mode=policy.mode,
                aggregate=policy.aggregate,
                host_scores=[],
                imbalance=0.0,
                imbalance_detected=False,
                timestamp=now,
                evaluation_duration_ms=(time.monotonic() - start) * 1000,
                skipped=True,
                skip_reason=(
                    f"Untrustworthy data (health={result.health.value}): "
                    f"{'; '.join(result.warnings)}"
                ),
            )

        # Filter to only hosts in the aggregate
        filtered = {
            host: score
            for host, score in result.series.items()
            if host in expected_hosts
        }

        if not filtered:
            return PolicyResult(
                policy_name=policy.name,
                mode=policy.mode,
                aggregate=policy.aggregate,
                host_scores=[],
                imbalance=0.0,
                imbalance_detected=False,
                timestamp=now,
                evaluation_duration_ms=(time.monotonic() - start) * 1000,
                skipped=True,
                skip_reason="No matching host data from Prometheus.",
            )

        host_scores = self._normalize_scores(filtered)
        imbalance = self._compute_imbalance(host_scores)
        imbalance_detected = imbalance > policy.threshold

        duration_ms = (time.monotonic() - start) * 1000

        LOG.info(
            "Policy '%s' aggregate='%s': imbalance=%.3f threshold=%.3f detected=%s (%.1fms)",
            policy.name,
            policy.aggregate,
            imbalance,
            policy.threshold,
            imbalance_detected,
            duration_ms,
        )

        return PolicyResult(
            policy_name=policy.name,
            mode=policy.mode,
            aggregate=policy.aggregate,
            host_scores=host_scores,
            imbalance=imbalance,
            imbalance_detected=imbalance_detected,
            timestamp=now,
            evaluation_duration_ms=duration_ms,
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

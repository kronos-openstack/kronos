"""Tests for the policy scorer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from kronos.clients.prometheus import PrometheusHealth, QueryResult
from kronos.common.exceptions import AggregateNotFound, PolicyEvaluationError
from kronos.engine.scorer import PolicyScorer
from kronos.engine.types import HostScore
from kronos.policies.models import PolicyConfig, PolicyMode


def _make_policy(**overrides) -> PolicyConfig:
    defaults = {
        "name": "test-policy",
        "mode": "spread",
        "aggregate": "test-agg",
        "imbalance_query": "test_metric",
        "threshold": 0.15,
        "cooldown": "10m",
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_query_result(
    series: dict[str, float],
    health: PrometheusHealth = PrometheusHealth.HEALTHY,
    missing: set[str] | None = None,
) -> QueryResult:
    return QueryResult(
        query="test",
        timestamp=datetime.now(tz=UTC),
        health=health,
        series=series,
        missing_labels=missing or set(),
        warnings=["stale"] if health != PrometheusHealth.HEALTHY else [],
    )


@pytest.fixture()
def mock_prometheus():
    return MagicMock()


@pytest.fixture()
def mock_nova():
    return MagicMock()


@pytest.fixture()
def scorer(mock_prometheus, mock_nova):
    return PolicyScorer(mock_prometheus, mock_nova)


class TestPolicyScorerEvaluate:
    def test_balanced_hosts(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2", "h3"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"h1": 0.50, "h2": 0.52, "h3": 0.48}
        )

        policy = _make_policy(threshold=0.15)
        result = scorer.evaluate(policy)

        assert not result.imbalance_detected
        assert not result.skipped
        assert len(result.host_scores) == 3
        assert result.policy_name == "test-policy"
        assert result.mode == PolicyMode.SPREAD

    def test_imbalanced_hosts(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2", "h3"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"h1": 0.90, "h2": 0.20, "h3": 0.50}
        )

        policy = _make_policy(threshold=0.15)
        result = scorer.evaluate(policy)

        assert result.imbalance_detected
        assert result.imbalance == pytest.approx(0.70)

    def test_empty_aggregate(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = []

        result = scorer.evaluate(_make_policy())

        assert result.skipped
        assert "no hosts" in result.skip_reason.lower()

    def test_untrustworthy_data_skipped(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"h1": 0.5},
            health=PrometheusHealth.PARTIAL,
            missing={"h2"},
        )

        result = scorer.evaluate(_make_policy())

        assert result.skipped
        assert "untrustworthy" in result.skip_reason.lower()

    def test_nova_failure_raises(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.side_effect = AggregateNotFound(aggregate="bad")

        with pytest.raises(PolicyEvaluationError, match="Failed to get aggregate"):
            scorer.evaluate(_make_policy(aggregate="bad"))

    def test_no_matching_hosts_in_prometheus(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"other-host": 0.5}
        )

        result = scorer.evaluate(_make_policy())

        assert result.skipped
        assert "no matching" in result.skip_reason.lower()

    def test_evaluation_duration_tracked(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1"]
        mock_prometheus.instant_query.return_value = _make_query_result({"h1": 0.5})

        result = scorer.evaluate(_make_policy())
        assert result.evaluation_duration_ms >= 0

    def test_scores_sorted_descending(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2", "h3"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"h1": 0.30, "h2": 0.80, "h3": 0.50}
        )

        result = scorer.evaluate(_make_policy())
        raw_scores = [hs.raw_score for hs in result.host_scores]
        assert raw_scores == sorted(raw_scores, reverse=True)

    def test_filters_non_aggregate_hosts(self, scorer, mock_nova, mock_prometheus):
        mock_nova.get_aggregate_hosts.return_value = ["h1", "h2"]
        mock_prometheus.instant_query.return_value = _make_query_result(
            {"h1": 0.5, "h2": 0.6, "extra-host": 0.9}
        )

        result = scorer.evaluate(_make_policy())
        host_names = {hs.host for hs in result.host_scores}
        assert host_names == {"h1", "h2"}


class TestNormalizeScores:
    def test_basic_normalization(self):
        raw = {"h1": 10.0, "h2": 20.0, "h3": 30.0}
        scores = PolicyScorer._normalize_scores(raw)

        assert len(scores) == 3
        normalized = {s.host: s.normalized_score for s in scores}
        assert normalized["h1"] == pytest.approx(0.0)
        assert normalized["h3"] == pytest.approx(1.0)
        assert normalized["h2"] == pytest.approx(0.5)

    def test_identical_values(self):
        raw = {"h1": 5.0, "h2": 5.0}
        scores = PolicyScorer._normalize_scores(raw)

        # With identical values, spread=0.5 (fallback), all normalize to 0.0
        for s in scores:
            assert s.normalized_score == pytest.approx(0.0)

    def test_single_host(self):
        raw = {"h1": 42.0}
        scores = PolicyScorer._normalize_scores(raw)
        assert len(scores) == 1
        assert scores[0].raw_score == 42.0


class TestComputeImbalance:
    def test_no_scores(self):
        assert PolicyScorer._compute_imbalance([]) == 0.0

    def test_single_score(self):
        scores = [HostScore(host="h1", raw_score=0.5, normalized_score=1.0)]
        assert PolicyScorer._compute_imbalance(scores) == 0.0

    def test_imbalance_calculation(self):
        scores = [
            HostScore(host="h1", raw_score=0.8, normalized_score=1.0),
            HostScore(host="h2", raw_score=0.3, normalized_score=0.0),
        ]
        assert PolicyScorer._compute_imbalance(scores) == pytest.approx(0.5)

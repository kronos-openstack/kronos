"""Tests for the engine control loop."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from kronos.engine.loop import EngineLoop
from kronos.engine.types import CycleReport, HostScore, MigrationPlan, MigrationStep, PolicyResult
from kronos.policies.models import PoliciesConfig, PolicyConfig, PolicyMode


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "test-policy",
        "mode": "spread",
        "aggregate": "test-agg",
        "imbalance_query": "test_metric",
        "threshold": 0.15,
        "cooldown": "10m",
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_policies_config(*policies: PolicyConfig) -> PoliciesConfig:
    if not policies:
        policies = (_make_policy(),)
    return PoliciesConfig(policies=list(policies))


def _make_policy_result(
    policy_name: str = "test-policy",
    imbalance_detected: bool = False,
    skipped: bool = False,
) -> PolicyResult:
    return PolicyResult(
        policy_name=policy_name,
        mode=PolicyMode.SPREAD,
        aggregate="test-agg",
        host_scores=[],
        imbalance=0.05,
        imbalance_detected=imbalance_detected,
        timestamp=datetime.now(tz=UTC),
        evaluation_duration_ms=10.0,
        skipped=skipped,
        skip_reason="test skip" if skipped else "",
    )


def _make_imbalanced_result() -> PolicyResult:
    return PolicyResult(
        policy_name="test-policy",
        mode=PolicyMode.SPREAD,
        aggregate="test-agg",
        host_scores=[
            HostScore(host="h1", raw_score=0.8, normalized_score=1.0),
            HostScore(host="h2", raw_score=0.2, normalized_score=0.0),
        ],
        imbalance=0.6,
        imbalance_detected=True,
        timestamp=datetime.now(tz=UTC),
        evaluation_duration_ms=10.0,
    )


@pytest.fixture()
def mock_engine():
    """Create an EngineLoop with all dependencies mocked."""
    with (
        patch("kronos.engine.loop.PrometheusClient"),
        patch("kronos.engine.loop.NovaClient"),
        patch("kronos.engine.loop.PolicyScorer") as mock_scorer_cls,
        patch("kronos.engine.loop.VmProfiler") as mock_profiler_cls,
        patch("kronos.engine.loop.ConstraintChecker"),
        patch("kronos.engine.loop.Planner") as mock_planner_cls,
    ):
        conf = MagicMock()
        conf.engine.evaluation_interval = 10
        conf.engine.dry_run = True
        conf.engine.policies_file = "/etc/kronos/policies.yaml"
        conf.engine.instance_cooldown = 900

        engine = EngineLoop(conf)
        engine._scorer = mock_scorer_cls.return_value
        engine._profiler = mock_profiler_cls.return_value
        engine._planner = mock_planner_cls.return_value
        yield engine


class TestRunCycle:
    def test_evaluates_enabled_policies(self, mock_engine: EngineLoop) -> None:
        p1 = _make_policy(name="enabled-policy")
        p2 = _make_policy(name="disabled-policy", enabled=False)
        policies = _make_policies_config(p1, p2)

        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, dry_run=True)

        assert isinstance(report, CycleReport)
        assert len(report.policy_results) == 1
        mock_engine._scorer.evaluate.assert_called_once_with(p1)

    def test_captures_evaluation_errors(self, mock_engine: EngineLoop) -> None:
        policy = _make_policy()
        policies = _make_policies_config(policy)

        mock_engine._scorer.evaluate.side_effect = Exception("boom")

        report = mock_engine._run_cycle(policies, dry_run=True)

        assert len(report.errors) == 1
        assert "boom" in report.errors[0]
        assert len(report.policy_results) == 0

    def test_cycle_number_increments(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        r1 = mock_engine._run_cycle(policies, dry_run=True)
        r2 = mock_engine._run_cycle(policies, dry_run=True)

        assert r1.cycle_number == 1
        assert r2.cycle_number == 2

    def test_dry_run_flag_propagated(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, dry_run=True)
        assert report.dry_run is True

    def test_multiple_policies(self, mock_engine: EngineLoop) -> None:
        p1 = _make_policy(name="policy-a")
        p2 = _make_policy(name="policy-b")
        policies = _make_policies_config(p1, p2)

        mock_engine._scorer.evaluate.side_effect = [
            _make_policy_result(policy_name="policy-a"),
            _make_policy_result(policy_name="policy-b"),
        ]

        report = mock_engine._run_cycle(policies, dry_run=True)
        assert len(report.policy_results) == 2

    def test_completed_at_after_started_at(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, dry_run=True)
        assert report.completed_at >= report.started_at


class TestEvaluatePolicy:
    def test_balanced_skips_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_policy_result()
        result = mock_engine._evaluate_policy(_make_policy(), dry_run=True)

        assert result.migration_plan is None
        mock_engine._profiler.collect.assert_not_called()
        mock_engine._planner.plan.assert_not_called()

    def test_skipped_skips_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_policy_result(skipped=True)
        result = mock_engine._evaluate_policy(_make_policy(), dry_run=True)

        assert result.migration_plan is None
        mock_engine._profiler.collect.assert_not_called()

    def test_imbalanced_triggers_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        mock_engine._profiler.collect.return_value = {"v1": MagicMock()}
        mock_engine._planner.plan.return_value = MigrationPlan(
            policy_name="test-policy", aggregate="test-agg",
        )

        result = mock_engine._evaluate_policy(_make_policy(), dry_run=True)

        mock_engine._profiler.collect.assert_called_once()
        mock_engine._planner.plan.assert_called_once()
        assert result.migration_plan is not None

    def test_no_vm_profiles_skips_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        mock_engine._profiler.collect.return_value = {}

        result = mock_engine._evaluate_policy(_make_policy(), dry_run=True)

        mock_engine._planner.plan.assert_not_called()
        assert result.migration_plan is None


class TestLogReport:
    def test_logs_without_error(self, mock_engine: EngineLoop) -> None:
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            policy_results=[_make_policy_result()],
        )
        mock_engine._log_report(report)

    def test_logs_skipped(self, mock_engine: EngineLoop) -> None:
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            policy_results=[_make_policy_result(skipped=True)],
        )
        mock_engine._log_report(report)

    def test_logs_imbalance_with_plan(self, mock_engine: EngineLoop) -> None:
        result = _make_imbalanced_result()
        result.migration_plan = MigrationPlan(
            policy_name="test-policy",
            aggregate="test-agg",
            steps=[
                MigrationStep(
                    instance_uuid="uuid-1",
                    instance_name="vm-1",
                    from_host="h1",
                    to_host="h2",
                    weight=0.2,
                    improvement=0.1,
                ),
            ],
            initial_imbalance=0.6,
            projected_imbalance=0.3,
        )
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            policy_results=[result],
        )
        mock_engine._log_report(report)

    def test_logs_imbalance_no_plan(self, mock_engine: EngineLoop) -> None:
        result = _make_imbalanced_result()
        result.migration_plan = None
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            policy_results=[result],
        )
        mock_engine._log_report(report)


class TestSignalHandling:
    def test_stop_sets_running_false(self, mock_engine: EngineLoop) -> None:
        mock_engine._running = True
        mock_engine.stop()
        assert mock_engine._running is False

    def test_signal_handler(self, mock_engine: EngineLoop) -> None:
        import signal

        mock_engine._running = True
        mock_engine._handle_signal(signal.SIGTERM, None)
        assert mock_engine._running is False


class TestStartLoop:
    def test_start_runs_one_cycle_then_stops(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())

        with patch("kronos.engine.loop.load_policies", return_value=policies):
            mock_engine._scorer.evaluate.return_value = _make_policy_result()

            def stop_after_sleep(interval: int) -> None:
                mock_engine._running = False

            with patch("kronos.engine.loop.time.sleep", side_effect=stop_after_sleep):
                mock_engine.start()

        assert mock_engine._cycle_count == 1

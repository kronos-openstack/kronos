"""Tests for the engine control loop."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from kronos.clients.nova import ComputeService
from kronos.engine.cooldown import QUARANTINE_FOREVER
from kronos.engine.loop import EngineLoop
from kronos.engine.types import (
    AggregateResult,
    CycleReport,
    HostScore,
    MigrationPhase,
    MigrationPlan,
    MigrationStep,
    PolicyResult,
    VmProfile,
)
from kronos.policies.models import PoliciesConfig, PolicyConfig, PolicyMode


def _make_compute_service(
    host: str,
    state: str = "up",
    status: str = "enabled",
    zone: str = "nova",
) -> ComputeService:
    return ComputeService(
        host=host,
        binary="nova-compute",
        state=state,
        status=status,
        zone=zone,
    )


def _make_policy(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "name": "test-policy",
        "mode": "spread",
        "weight": 1.0,
        "imbalance_query": "test_metric",
        "threshold": 0.15,
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
    host_scores: list[HostScore] | None = None,
) -> PolicyResult:
    return PolicyResult(
        policy_name=policy_name,
        mode=PolicyMode.SPREAD,
        host_scores=host_scores or [],
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
    """Create an EngineLoop with all external dependencies mocked."""
    with (
        patch("kronos.engine.loop.PrometheusClient"),
        patch("kronos.engine.loop.NovaClient") as mock_nova_cls,
        patch("kronos.engine.loop.PolicyScorer") as mock_scorer_cls,
        patch("kronos.engine.loop.VmProfiler") as mock_profiler_cls,
        patch("kronos.engine.loop.ConstraintChecker"),
        patch("kronos.engine.loop.Planner") as mock_planner_cls,
        patch("kronos.engine.loop.AffinityEnforcer") as mock_enforcer_cls,
        patch("kronos.engine.loop.Evacuator") as mock_evacuator_cls,
    ):
        conf = MagicMock()
        conf.engine.evaluation_interval = 10
        conf.engine.dry_run = True
        conf.engine.policies_file = "/etc/kronos/policies.yaml"
        conf.engine.instance_cooldown = 900
        conf.engine.cooldown = 600
        conf.engine.aggregates = ["test-agg"]
        conf.engine.availability_zone = "nova"
        conf.engine.include_unassigned_hosts = False
        conf.engine.enforce_hard_affinity = False
        conf.engine.enforce_soft_affinity = False
        conf.engine.evacuate_disabled_hosts = False

        engine = EngineLoop(conf)
        engine._nova = mock_nova_cls.return_value
        engine._scorer = mock_scorer_cls.return_value
        engine._profiler = mock_profiler_cls.return_value
        engine._planner = mock_planner_cls.return_value

        # Default enforcer: disabled, returns an empty plan plus the
        # scores/vms_by_host passed through untouched.
        enforcer = mock_enforcer_cls.return_value
        enforcer.enabled = False

        def _enforce_passthrough(
            *, scores=None, vms_by_host=None, budget=0, **_kwargs,
        ):
            return (
                MigrationPlan(
                    aggregate="test-agg", policy_names=["test-policy"],
                ),
                scores if scores is not None else {},
                vms_by_host if vms_by_host is not None else {},
                budget,
            )

        enforcer.enforce.side_effect = _enforce_passthrough
        engine._enforcer = enforcer

        # Default evacuator: disabled, passes scores/vms_by_host through.
        evacuator = mock_evacuator_cls.return_value
        evacuator.enabled = False

        def _evacuate_passthrough(
            *, scores=None, vms_by_host=None, budget=0, **_kwargs,
        ):
            return (
                MigrationPlan(
                    aggregate="test-agg", policy_names=["test-policy"],
                ),
                scores if scores is not None else {},
                vms_by_host if vms_by_host is not None else {},
                budget,
            )

        evacuator.evacuate.side_effect = _evacuate_passthrough
        engine._evacuator = evacuator

        # Default: one host in the test aggregate, both up + enabled.
        engine._nova.get_hosts_in_aggregate.return_value = ["h1", "h2"]
        engine._nova.list_compute_services.return_value = [
            _make_compute_service("h1"),
            _make_compute_service("h2"),
        ]
        yield engine


class TestRunCycle:
    def test_evaluates_aggregates(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy(name="enabled-policy"))
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)

        assert isinstance(report, CycleReport)
        assert len(report.aggregate_results) == 1

    def test_captures_evaluation_errors(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._nova.get_hosts_in_aggregate.side_effect = Exception("boom")

        report = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)

        assert len(report.errors) == 1
        assert "boom" in report.errors[0]

    def test_cycle_number_increments(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        r1 = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)
        r2 = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)

        assert r1.cycle_number == 1
        assert r2.cycle_number == 2


def _vm(uuid: str, host: str = "h1") -> VmProfile:
    return VmProfile(instance_uuid=uuid, instance_name=uuid, host=host)


class TestFilterUnavailableVms:
    def test_passes_through_when_no_restrictions(
        self, mock_engine: EngineLoop,
    ) -> None:
        profiles = {"vm-1": _vm("vm-1"), "vm-2": _vm("vm-2")}
        kept = mock_engine._filter_unavailable_vms(profiles, "test-agg")
        assert kept == profiles

    def test_drops_quarantined(self, mock_engine: EngineLoop) -> None:
        profiles = {"vm-1": _vm("vm-1"), "vm-bad": _vm("vm-bad")}
        mock_engine._cooldown.quarantine_instance("vm-bad", QUARANTINE_FOREVER)
        kept = mock_engine._filter_unavailable_vms(profiles, "test-agg")
        assert "vm-bad" not in kept
        assert "vm-1" in kept

    def test_drops_cooling(self, mock_engine: EngineLoop) -> None:
        profiles = {"vm-1": _vm("vm-1"), "vm-2": _vm("vm-2")}
        mock_engine._cooldown.record_plan_emission("test-agg", ["vm-2"])
        kept = mock_engine._filter_unavailable_vms(profiles, "test-agg")
        assert "vm-2" not in kept
        assert "vm-1" in kept

    def test_drops_both_cooling_and_quarantined(
        self, mock_engine: EngineLoop,
    ) -> None:
        profiles = {
            "vm-ok": _vm("vm-ok"),
            "vm-cool": _vm("vm-cool"),
            "vm-q": _vm("vm-q"),
        }
        mock_engine._cooldown.record_plan_emission("test-agg", ["vm-cool"])
        mock_engine._cooldown.quarantine_instance("vm-q", 600.0)
        kept = mock_engine._filter_unavailable_vms(profiles, "test-agg")
        assert set(kept) == {"vm-ok"}

    def test_empty_input_returns_empty(
        self, mock_engine: EngineLoop,
    ) -> None:
        assert mock_engine._filter_unavailable_vms({}, "test-agg") == {}

    def test_dry_run_flag_propagated(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)
        assert report.dry_run is True

    def test_multi_aggregate(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(
            policies, ["agg-a", "agg-b"], dry_run=True,
        )
        assert len(report.aggregate_results) == 2

    def test_completed_at_after_started_at(self, mock_engine: EngineLoop) -> None:
        policies = _make_policies_config(_make_policy())
        mock_engine._scorer.evaluate.return_value = _make_policy_result()

        report = mock_engine._run_cycle(policies, ["test-agg"], dry_run=True)
        assert report.completed_at >= report.started_at


class TestEvaluateAggregate:
    def test_no_hosts_returns_empty(self, mock_engine: EngineLoop) -> None:
        mock_engine._nova.get_hosts_in_aggregate.return_value = []
        result = mock_engine._evaluate_aggregate(
            "empty-agg", [_make_policy()], dry_run=True,
            services={"h1": _make_compute_service("h1")},
        )
        assert result.aggregate == "empty-agg"
        assert not result.imbalance_detected

    def test_balanced_skips_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_policy_result()
        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1"),
                "h2": _make_compute_service("h2"),
            },
        )

        assert result.migration_plan is None
        mock_engine._profiler.collect.assert_not_called()
        mock_engine._planner.plan.assert_not_called()

    def test_skipped_policy(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_policy_result(skipped=True)
        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1"),
                "h2": _make_compute_service("h2"),
            },
        )

        assert not result.imbalance_detected
        mock_engine._profiler.collect.assert_not_called()

    def test_imbalanced_triggers_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        mock_engine._profiler.collect.return_value = {"v1": MagicMock()}
        mock_engine._planner.plan.return_value = MigrationPlan(aggregate="test-agg")

        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1"),
                "h2": _make_compute_service("h2"),
            },
        )

        mock_engine._profiler.collect.assert_called_once()
        mock_engine._planner.plan.assert_called_once()
        assert result.migration_plan is not None
        assert result.imbalance_detected

    def test_drops_hosts_outside_configured_az(
        self, mock_engine: EngineLoop,
    ) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1", zone="other"),
                "h2": _make_compute_service("h2", zone="other"),
            },
        )
        assert result.aggregate == "test-agg"
        mock_engine._profiler.collect.assert_not_called()
        mock_engine._planner.plan.assert_not_called()

    def test_keeps_hosts_in_configured_az(
        self, mock_engine: EngineLoop,
    ) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        mock_engine._profiler.collect.return_value = {"v1": MagicMock()}
        mock_engine._planner.plan.return_value = MigrationPlan(aggregate="test-agg")
        mock_engine._nova.get_hosts_in_aggregate.return_value = ["h1", "h2", "h3"]

        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1", zone="nova"),
                "h2": _make_compute_service("h2", zone="other"),
                "h3": _make_compute_service("h3", zone="nova"),
            },
        )

        called_hosts = mock_engine._scorer.evaluate.call_args_list[0].args[1]
        assert called_hosts == ["h1", "h3"]
        assert result.imbalance_detected

    def test_no_vm_profiles_skips_planner(self, mock_engine: EngineLoop) -> None:
        mock_engine._scorer.evaluate.return_value = _make_imbalanced_result()
        mock_engine._profiler.collect.return_value = {}

        result = mock_engine._evaluate_aggregate(
            "test-agg", [_make_policy()], dry_run=True,
            services={
                "h1": _make_compute_service("h1"),
                "h2": _make_compute_service("h2"),
            },
        )

        mock_engine._planner.plan.assert_not_called()
        assert result.migration_plan is None


class TestResolveAggregates:
    def test_aggregates_only(self, mock_engine: EngineLoop) -> None:
        mock_engine._conf.engine.aggregates = ["a", "b"]
        mock_engine._conf.engine.include_unassigned_hosts = False
        assert mock_engine._resolve_aggregates() == ["a", "b"]

    def test_only_unassigned(self, mock_engine: EngineLoop) -> None:
        mock_engine._conf.engine.aggregates = []
        mock_engine._conf.engine.include_unassigned_hosts = True
        assert mock_engine._resolve_aggregates() == [None]

    def test_both(self, mock_engine: EngineLoop) -> None:
        mock_engine._conf.engine.aggregates = ["a"]
        mock_engine._conf.engine.include_unassigned_hosts = True
        assert mock_engine._resolve_aggregates() == ["a", None]

    def test_neither(self, mock_engine: EngineLoop) -> None:
        mock_engine._conf.engine.aggregates = []
        mock_engine._conf.engine.include_unassigned_hosts = False
        assert mock_engine._resolve_aggregates() == []


class TestLogReport:
    def test_logs_without_error(self, mock_engine: EngineLoop) -> None:
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            aggregate_results=[
                AggregateResult(
                    aggregate="agg",
                    policy_results=[_make_policy_result()],
                ),
            ],
        )
        mock_engine._log_report(report)

    def test_logs_skipped(self, mock_engine: EngineLoop) -> None:
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            aggregate_results=[
                AggregateResult(
                    aggregate="agg",
                    policy_results=[_make_policy_result(skipped=True)],
                ),
            ],
        )
        mock_engine._log_report(report)

    def test_logs_imbalance_with_plan(self, mock_engine: EngineLoop) -> None:
        ar = AggregateResult(
            aggregate="agg",
            policy_results=[_make_imbalanced_result()],
            combined_imbalance=0.6,
            imbalance_detected=True,
            migration_plan=MigrationPlan(
                aggregate="agg",
                policy_names=["test-policy"],
                steps=[
                    MigrationStep(
                        instance_uuid="uuid-1",
                        instance_name="vm-1",
                        from_host="h1",
                        to_host="h2",
                        improvement=0.1,
                        phase=MigrationPhase.SPREAD,
                    ),
                ],
                initial_imbalance=0.6,
                projected_imbalance=0.3,
            ),
        )
        report = CycleReport(
            cycle_number=1,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            aggregate_results=[ar],
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

            def stop_after_wait(*_a: object, **_kw: object) -> bool:
                mock_engine._running = False
                return True

            # Inter-cycle pause is now an Event.wait, not time.sleep.
            with patch.object(
                mock_engine._wakeup, "wait", side_effect=stop_after_wait,
            ):
                mock_engine.start()

        assert mock_engine._cycle_count == 1

    def test_signal_unblocks_intercycle_wait(
        self, mock_engine: EngineLoop,
    ) -> None:
        """Signal handler must unblock the inter-cycle wait immediately."""
        import signal as _signal
        policies = _make_policies_config(_make_policy())

        with patch("kronos.engine.loop.load_policies", return_value=policies):
            mock_engine._scorer.evaluate.return_value = _make_policy_result()

            def fire_signal(*_a: object, **_kw: object) -> bool:
                mock_engine._handle_signal(_signal.SIGTERM, None)
                return True

            with patch.object(
                mock_engine._wakeup, "wait", side_effect=fire_signal,
            ):
                mock_engine.start()

        assert mock_engine._running is False
        assert mock_engine._cycle_count == 1

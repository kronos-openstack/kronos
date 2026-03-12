"""Tests for Pydantic policy models."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from kronos.policies.models import (
    PoliciesConfig,
    PolicyConfig,
    PolicyMode,
    VmProfileFallback,
    VmProfileLabelType,
    parse_duration,
)


class TestParseDuration:
    def test_timedelta_passthrough(self):
        td = timedelta(minutes=5)
        assert parse_duration(td) == td

    def test_int_seconds(self):
        assert parse_duration(300) == timedelta(seconds=300)

    def test_float_seconds(self):
        assert parse_duration(60.5) == timedelta(seconds=60.5)

    def test_string_minutes(self):
        assert parse_duration("10m") == timedelta(minutes=10)

    def test_string_hours(self):
        assert parse_duration("1h") == timedelta(hours=1)

    def test_string_seconds(self):
        assert parse_duration("30s") == timedelta(seconds=30)

    def test_string_combined(self):
        result = parse_duration("1h30m")
        assert result == timedelta(hours=1, minutes=30)

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Cannot parse duration"):
            parse_duration("not-a-duration")


class TestPolicyConfig:
    def test_minimal_valid_spread(self, sample_policy_dict):
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.name == "test-policy"
        assert policy.mode == PolicyMode.SPREAD
        assert policy.aggregate == "test-aggregate"
        assert policy.imbalance_query == "up"
        assert policy.threshold == 0.15
        assert policy.cooldown == timedelta(minutes=10)
        assert policy.enabled is True

    def test_defaults(self, sample_policy_dict):
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.weight == 1.0
        assert policy.host_label == "host"
        assert policy.vm_profile_query is None
        assert policy.vm_profile_label == "instance_name"
        assert policy.vm_profile_label_type == VmProfileLabelType.NOVA_INTERNAL_NAME
        assert policy.vm_profile_fallback == VmProfileFallback.SKIP
        assert policy.capacity_query is None
        assert policy.capacity_threshold == 0.80
        assert policy.bin_pack_drain_threshold == 0.30
        assert policy.max_migrations_per_cycle == 3
        assert policy.min_sustained_minutes == 0

    def test_pack_mode_valid(self, sample_pack_policy_dict):
        policy = PolicyConfig(**sample_pack_policy_dict)
        assert policy.mode == PolicyMode.PACK
        assert policy.capacity_query == "capacity_metric"

    def test_pack_mode_requires_capacity_query(self, sample_policy_dict):
        sample_policy_dict["mode"] = "pack"
        with pytest.raises(ValidationError, match="capacity_query"):
            PolicyConfig(**sample_policy_dict)

    def test_drain_must_be_below_capacity(self):
        with pytest.raises(ValidationError, match="bin_pack_drain_threshold"):
            PolicyConfig(
                name="bad-thresholds",
                mode="pack",
                aggregate="agg",
                imbalance_query="up",
                capacity_query="cap",
                capacity_threshold=0.50,
                bin_pack_drain_threshold=0.60,
            )

    def test_drain_equal_to_capacity_rejected(self):
        with pytest.raises(ValidationError, match="bin_pack_drain_threshold"):
            PolicyConfig(
                name="equal-thresholds",
                mode="pack",
                aggregate="agg",
                imbalance_query="up",
                capacity_query="cap",
                capacity_threshold=0.50,
                bin_pack_drain_threshold=0.50,
            )

    def test_invalid_name_uppercase(self):
        with pytest.raises(ValidationError, match="name"):
            PolicyConfig(
                name="BadName",
                mode="spread",
                aggregate="agg",
                imbalance_query="up",
            )

    def test_invalid_name_starts_with_number(self):
        with pytest.raises(ValidationError, match="name"):
            PolicyConfig(
                name="1-bad",
                mode="spread",
                aggregate="agg",
                imbalance_query="up",
            )

    def test_valid_name_with_hyphens_underscores(self):
        policy = PolicyConfig(
            name="my-cool_policy-1",
            mode="spread",
            aggregate="agg",
            imbalance_query="up",
        )
        assert policy.name == "my-cool_policy-1"

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            PolicyConfig(
                name="",
                mode="spread",
                aggregate="agg",
                imbalance_query="up",
            )

    def test_invalid_mode(self):
        with pytest.raises(ValidationError, match="mode"):
            PolicyConfig(
                name="bad-mode",
                mode="invalid",
                aggregate="agg",
                imbalance_query="up",
            )

    def test_threshold_range(self):
        with pytest.raises(ValidationError, match="threshold"):
            PolicyConfig(
                name="bad-threshold",
                mode="spread",
                aggregate="agg",
                imbalance_query="up",
                threshold=1.5,
            )

    def test_weight_must_be_positive(self):
        with pytest.raises(ValidationError, match="weight"):
            PolicyConfig(
                name="bad-weight",
                mode="spread",
                aggregate="agg",
                imbalance_query="up",
                weight=0.0,
            )

    def test_cooldown_string_parsing(self):
        policy = PolicyConfig(
            name="cooldown-test",
            mode="spread",
            aggregate="agg",
            imbalance_query="up",
            cooldown="1h",
        )
        assert policy.cooldown == timedelta(hours=1)

    def test_cooldown_int_seconds(self):
        policy = PolicyConfig(
            name="cooldown-int",
            mode="spread",
            aggregate="agg",
            imbalance_query="up",
            cooldown=300,
        )
        assert policy.cooldown == timedelta(seconds=300)

    def test_disabled_policy(self, sample_policy_dict):
        sample_policy_dict["enabled"] = False
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.enabled is False


class TestPoliciesConfig:
    def test_valid_policies_list(self, sample_policy_dict, sample_pack_policy_dict):
        config = PoliciesConfig(
            policies=[
                PolicyConfig(**sample_policy_dict),
                PolicyConfig(**sample_pack_policy_dict),
            ]
        )
        assert len(config.policies) == 2

    def test_duplicate_names_rejected(self, sample_policy_dict):
        dup = sample_policy_dict.copy()
        with pytest.raises(ValidationError, match="Duplicate policy names"):
            PoliciesConfig(
                policies=[
                    PolicyConfig(**sample_policy_dict),
                    PolicyConfig(**dup),
                ]
            )

    def test_empty_policies_rejected(self):
        with pytest.raises(ValidationError):
            PoliciesConfig(policies=[])

    def test_from_dict(self, sample_policy_dict):
        raw = {"policies": [sample_policy_dict]}
        config = PoliciesConfig.model_validate(raw)
        assert len(config.policies) == 1
        assert config.policies[0].name == "test-policy"

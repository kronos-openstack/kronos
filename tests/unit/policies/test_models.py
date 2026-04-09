"""Tests for Pydantic policy models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kronos.policies.models import (
    PoliciesConfig,
    PolicyConfig,
    PolicyMode,
    VmProfileFallback,
    VmProfileLabelType,
)


class TestPolicyConfig:
    def test_minimal_valid_spread(self, sample_policy_dict):
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.name == "test-policy"
        assert policy.mode == PolicyMode.SPREAD
        assert policy.weight == 1.0
        assert policy.imbalance_query == "up"
        assert policy.threshold == 0.15
        assert policy.enabled is True

    def test_defaults(self, sample_policy_dict):
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.host_label == "host"
        assert policy.vm_profile_query is None
        assert policy.vm_profile_label == "instance_name"
        assert policy.vm_profile_label_type == VmProfileLabelType.NOVA_INTERNAL_NAME
        assert policy.vm_profile_fallback == VmProfileFallback.SKIP
        assert policy.capacity_query is None
        assert policy.capacity_threshold == 0.80
        assert policy.max_migrations_per_cycle == 3

    def test_pack_mode_valid(self, sample_pack_policy_dict):
        policy = PolicyConfig(**sample_pack_policy_dict)
        assert policy.mode == PolicyMode.PACK
        assert policy.capacity_query == "capacity_metric"

    def test_pack_mode_requires_capacity_query(self, sample_policy_dict):
        sample_policy_dict["mode"] = "pack"
        with pytest.raises(ValidationError, match="capacity_query"):
            PolicyConfig(**sample_policy_dict)

    def test_invalid_name_uppercase(self):
        with pytest.raises(ValidationError, match="name"):
            PolicyConfig(
                name="BadName",
                mode="spread",
                weight=1.0,
                imbalance_query="up",
            )

    def test_invalid_name_starts_with_number(self):
        with pytest.raises(ValidationError, match="name"):
            PolicyConfig(
                name="1-bad",
                mode="spread",
                weight=1.0,
                imbalance_query="up",
            )

    def test_valid_name_with_hyphens_underscores(self):
        policy = PolicyConfig(
            name="my-cool_policy-1",
            mode="spread",
            weight=1.0,
            imbalance_query="up",
        )
        assert policy.name == "my-cool_policy-1"

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            PolicyConfig(
                name="",
                mode="spread",
                weight=1.0,
                imbalance_query="up",
            )

    def test_invalid_mode(self):
        with pytest.raises(ValidationError, match="mode"):
            PolicyConfig(
                name="bad-mode",
                mode="invalid",
                weight=1.0,
                imbalance_query="up",
            )

    def test_threshold_range(self):
        with pytest.raises(ValidationError, match="threshold"):
            PolicyConfig(
                name="bad-threshold",
                mode="spread",
                weight=1.0,
                imbalance_query="up",
                threshold=1.5,
            )

    def test_weight_must_be_zero_or_positive(self):
        policy = PolicyConfig(
            name="zero-weight",
            mode="spread",
            weight=0.0,
            imbalance_query="up",
        )
        assert policy.weight == 0.0

    def test_weight_must_not_exceed_one(self):
        with pytest.raises(ValidationError, match="weight"):
            PolicyConfig(
                name="huge-weight",
                mode="spread",
                weight=1.5,
                imbalance_query="up",
            )

    def test_disabled_policy(self, sample_policy_dict):
        sample_policy_dict["enabled"] = False
        policy = PolicyConfig(**sample_policy_dict)
        assert policy.enabled is False


class TestPoliciesConfig:
    def test_valid_single_policy(self, sample_policy_dict):
        config = PoliciesConfig(policies=[PolicyConfig(**sample_policy_dict)])
        assert len(config.policies) == 1

    def test_valid_two_spread_policies_summing_to_one(self, sample_policy_dict):
        a = dict(sample_policy_dict, name="alpha", weight=0.4)
        b = dict(sample_policy_dict, name="beta", weight=0.6)
        config = PoliciesConfig(
            policies=[PolicyConfig(**a), PolicyConfig(**b)],
        )
        assert len(config.policies) == 2

    def test_weights_not_summing_to_one_rejected(self, sample_policy_dict):
        a = dict(sample_policy_dict, name="alpha", weight=0.3)
        b = dict(sample_policy_dict, name="beta", weight=0.3)
        with pytest.raises(ValidationError, match=r"weights must sum to 1\.0"):
            PoliciesConfig(policies=[PolicyConfig(**a), PolicyConfig(**b)])

    def test_disabled_policy_weight_excluded_from_sum(self, sample_policy_dict):
        a = dict(sample_policy_dict, name="alpha", weight=1.0)
        b = dict(sample_policy_dict, name="beta", weight=0.7, enabled=False)
        config = PoliciesConfig(
            policies=[PolicyConfig(**a), PolicyConfig(**b)],
        )
        assert len(config.policies) == 2

    def test_mixed_modes_rejected(self, sample_policy_dict, sample_pack_policy_dict):
        with pytest.raises(ValidationError, match="share a mode"):
            PoliciesConfig(
                policies=[
                    PolicyConfig(**sample_policy_dict),
                    PolicyConfig(**sample_pack_policy_dict),
                ],
            )

    def test_duplicate_names_rejected(self, sample_policy_dict):
        dup = sample_policy_dict.copy()
        with pytest.raises(ValidationError, match="Duplicate policy names"):
            PoliciesConfig(
                policies=[
                    PolicyConfig(**sample_policy_dict),
                    PolicyConfig(**dup),
                ],
            )

    def test_empty_policies_rejected(self):
        with pytest.raises(ValidationError):
            PoliciesConfig(policies=[])

    def test_from_dict(self, sample_policy_dict):
        raw = {"policies": [sample_policy_dict]}
        config = PoliciesConfig.model_validate(raw)
        assert len(config.policies) == 1
        assert config.policies[0].name == "test-policy"

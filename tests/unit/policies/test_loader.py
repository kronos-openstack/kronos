"""Tests for policy YAML loader."""

from __future__ import annotations

import pytest

from kronos.common.exceptions import PolicyFileNotFound, PolicyValidationError
from kronos.policies.loader import load_policies


class TestLoadPolicies:
    def test_load_valid_file(self, valid_policies_path):
        config = load_policies(valid_policies_path)
        assert len(config.policies) == 2
        assert config.policies[0].name == "gpu-cpu-spread"
        assert config.policies[0].enabled is True
        assert config.policies[1].name == "std-memory-spread"
        assert config.policies[1].enabled is False

    def test_load_invalid_duplicate_names(self, invalid_policies_path):
        with pytest.raises(PolicyValidationError, match="Duplicate policy names"):
            load_policies(invalid_policies_path)

    def test_file_not_found(self, tmp_path):
        fake_path = tmp_path / "nonexistent.yaml"
        with pytest.raises(PolicyFileNotFound):
            load_policies(fake_path)

    def test_invalid_yaml_syntax(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("policies:\n  - name: [invalid yaml\n")
        with pytest.raises(PolicyValidationError, match="Invalid YAML"):
            load_policies(bad_yaml)

    def test_non_mapping_top_level(self, tmp_path):
        list_yaml = tmp_path / "list.yaml"
        list_yaml.write_text("- item1\n- item2\n")
        with pytest.raises(PolicyValidationError, match="Expected a YAML mapping"):
            load_policies(list_yaml)

    def test_missing_required_fields(self, tmp_path):
        incomplete = tmp_path / "incomplete.yaml"
        incomplete.write_text("policies:\n  - name: incomplete\n")
        with pytest.raises(PolicyValidationError):
            load_policies(incomplete)

    def test_accepts_string_path(self, valid_policies_path):
        config = load_policies(str(valid_policies_path))
        assert len(config.policies) == 2

    def test_policy_field_values(self, valid_policies_path):
        config = load_policies(valid_policies_path)
        gpu_policy = config.policies[0]
        assert gpu_policy.aggregate == "gpu-aggregate"
        assert gpu_policy.weight == 0.6
        assert gpu_policy.threshold == 0.15
        assert gpu_policy.max_migrations_per_cycle == 3

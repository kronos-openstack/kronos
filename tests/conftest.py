"""Shared test fixtures for Kronos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROMETHEUS_FIXTURES_DIR = FIXTURES_DIR / "prometheus_responses"


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture()
def valid_policies_path() -> Path:
    return FIXTURES_DIR / "policies_valid.yaml"


@pytest.fixture()
def invalid_policies_path() -> Path:
    return FIXTURES_DIR / "policies_invalid.yaml"


@pytest.fixture()
def prometheus_healthy_response() -> dict[str, Any]:
    return json.loads((PROMETHEUS_FIXTURES_DIR / "healthy.json").read_text())


@pytest.fixture()
def prometheus_stale_response() -> dict[str, Any]:
    return json.loads((PROMETHEUS_FIXTURES_DIR / "stale.json").read_text())


@pytest.fixture()
def prometheus_partial_response() -> dict[str, Any]:
    return json.loads((PROMETHEUS_FIXTURES_DIR / "partial.json").read_text())


@pytest.fixture()
def sample_policy_dict() -> dict[str, Any]:
    """Minimal valid policy dict for unit tests."""
    return {
        "name": "test-policy",
        "mode": "spread",
        "weight": 1.0,
        "imbalance_query": "up",
        "threshold": 0.15,
    }


@pytest.fixture()
def sample_pack_policy_dict() -> dict[str, Any]:
    """Valid pack-mode policy dict."""
    return {
        "name": "test-pack-policy",
        "mode": "pack",
        "weight": 1.0,
        "imbalance_query": "up",
        "capacity_query": "capacity_metric",
        "threshold": 0.10,
        "capacity_threshold": 0.80,
    }


@pytest.fixture()
def prometheus_conf(tmp_path):
    """Create a minimal oslo.config ConfigOpts with prometheus settings."""
    from oslo_config import cfg

    from kronos.common.config import register_opts

    conf = cfg.ConfigOpts()
    register_opts(conf)
    conf(
        [
            "--prometheus-url", "http://prometheus:9090",
        ],
        project="kronos-test",
    )
    return conf

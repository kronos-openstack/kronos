"""Tests for the Prometheus HTTP client."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import responses

from kronos.clients.prometheus import PrometheusClient, PrometheusHealth, QueryResult
from kronos.common.exceptions import (
    PrometheusQueryError,
    PrometheusUnreachableError,
)

FIXTURES_DIR = Path(__file__).parents[2] / "fixtures" / "prometheus_responses"

PROM_URL = "http://prometheus:9090"


def _make_conf(url: str = PROM_URL, **overrides) -> MagicMock:
    """Build a mock oslo.config with prometheus settings."""
    prom = MagicMock()
    prom.url = url
    prom.timeout = overrides.get("timeout", 10)
    prom.max_retries = overrides.get("max_retries", 1)
    prom.retry_backoff = overrides.get("retry_backoff", 0.1)
    prom.staleness_threshold = overrides.get("staleness_threshold", 300)
    prom.verify_ssl = overrides.get("verify_ssl", True)
    prom.ca_cert = overrides.get("ca_cert")
    prom.bearer_token = overrides.get("bearer_token")
    prom.bearer_token_file = overrides.get("bearer_token_file")

    conf = MagicMock()
    conf.prometheus = prom
    return conf


class TestPrometheusClientInit:
    def test_basic_init(self):
        conf = _make_conf()
        client = PrometheusClient(conf)
        assert client._base_url == PROM_URL

    def test_bearer_token(self):
        conf = _make_conf(bearer_token="my-token")
        client = PrometheusClient(conf)
        assert client._session.headers["Authorization"] == "Bearer my-token"

    def test_bearer_token_file(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("  file-token  \n")
        conf = _make_conf(bearer_token_file=str(token_file))
        client = PrometheusClient(conf)
        assert client._session.headers["Authorization"] == "Bearer file-token"

    def test_bearer_token_takes_precedence(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token")
        conf = _make_conf(bearer_token="direct-token", bearer_token_file=str(token_file))
        client = PrometheusClient(conf)
        assert client._session.headers["Authorization"] == "Bearer direct-token"

    def test_ssl_disabled(self):
        conf = _make_conf(verify_ssl=False)
        client = PrometheusClient(conf)
        assert client._session.verify is False

    def test_ca_cert(self):
        conf = _make_conf(ca_cert="/path/to/ca.pem")
        client = PrometheusClient(conf)
        assert client._session.verify == "/path/to/ca.pem"


class TestHealthCheck:
    @responses.activate
    def test_healthy(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/status/runtimeinfo",
            json={"status": "success"},
            status=200,
        )
        client = PrometheusClient(_make_conf())
        assert client.health_check() == PrometheusHealth.HEALTHY

    @responses.activate
    def test_unreachable(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/status/runtimeinfo",
            body=requests.ConnectionError("refused"),
        )
        client = PrometheusClient(_make_conf())
        assert client.health_check() == PrometheusHealth.UNREACHABLE

    @responses.activate
    def test_server_error(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/status/runtimeinfo",
            status=500,
        )
        client = PrometheusClient(_make_conf())
        assert client.health_check() == PrometheusHealth.UNREACHABLE


class TestInstantQuery:
    @responses.activate
    def test_healthy_query(self):
        fixture = json.loads((FIXTURES_DIR / "healthy.json").read_text())
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json=fixture,
            status=200,
        )

        client = PrometheusClient(_make_conf(staleness_threshold=999999999))
        result = client.instant_query("test_query")

        assert isinstance(result, QueryResult)
        assert result.health == PrometheusHealth.HEALTHY
        assert len(result.series) == 3
        assert result.series["compute-01"] == pytest.approx(0.45)
        assert result.series["compute-02"] == pytest.approx(0.52)
        assert result.series["compute-03"] == pytest.approx(0.38)
        assert result.is_trustworthy

    @responses.activate
    def test_stale_data(self):
        fixture = json.loads((FIXTURES_DIR / "stale.json").read_text())
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json=fixture,
            status=200,
        )

        client = PrometheusClient(_make_conf(staleness_threshold=300))
        result = client.instant_query("test_query")

        assert result.health == PrometheusHealth.STALE
        assert not result.is_trustworthy
        assert len(result.warnings) > 0

    @responses.activate
    def test_partial_data(self):
        fixture = json.loads((FIXTURES_DIR / "partial.json").read_text())
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json=fixture,
            status=200,
        )

        expected = {"compute-01", "compute-02", "compute-03"}
        client = PrometheusClient(_make_conf(staleness_threshold=999999999))
        result = client.instant_query(
            "test_query",
            expected_labels=expected,
        )

        assert result.health == PrometheusHealth.PARTIAL
        assert not result.is_trustworthy
        assert result.missing_labels == {"compute-02", "compute-03"}

    @responses.activate
    def test_custom_label_key(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"instance": "node1:9100"},
                            "value": [1700000000.0, "42"],
                        },
                    ],
                },
            },
            status=200,
        )

        client = PrometheusClient(_make_conf(staleness_threshold=999999999))
        result = client.instant_query("up", label_key="instance")

        assert "node1:9100" in result.series
        assert result.series["node1:9100"] == 42.0

    @responses.activate
    def test_non_vector_result_raises(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json={
                "status": "success",
                "data": {"resultType": "matrix", "result": []},
            },
            status=200,
        )

        client = PrometheusClient(_make_conf())
        with pytest.raises(PrometheusQueryError, match="Expected vector"):
            client.instant_query("test_query")

    @responses.activate
    def test_error_status_raises(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json={"status": "error", "error": "bad query"},
            status=200,
        )

        client = PrometheusClient(_make_conf())
        with pytest.raises(PrometheusQueryError, match="bad query"):
            client.instant_query("bad{")

    @responses.activate
    def test_http_error_raises(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            status=500,
            body="Internal Server Error",
        )

        client = PrometheusClient(_make_conf())
        with pytest.raises(PrometheusQueryError, match="HTTP 500"):
            client.instant_query("test_query")

    @responses.activate
    def test_connection_error_raises(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            body=requests.ConnectionError("refused"),
        )

        client = PrometheusClient(_make_conf(max_retries=0))
        with pytest.raises(PrometheusUnreachableError):
            client.instant_query("test_query")

    @responses.activate
    def test_non_numeric_values_skipped(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"host": "good"},
                            "value": [1700000000.0, "0.5"],
                        },
                        {
                            "metric": {"host": "bad"},
                            "value": [1700000000.0, "NaN"],
                        },
                    ],
                },
            },
            status=200,
        )

        client = PrometheusClient(_make_conf(staleness_threshold=999999999))
        result = client.instant_query("test_query")

        # NaN is a valid float, so both should be present
        assert "good" in result.series
        assert result.series["good"] == 0.5

    @responses.activate
    def test_missing_label_in_metric_skipped(self):
        responses.add(
            responses.GET,
            f"{PROM_URL}/api/v1/query",
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"job": "libvirt"},
                            "value": [1700000000.0, "0.5"],
                        },
                    ],
                },
            },
            status=200,
        )

        client = PrometheusClient(_make_conf(staleness_threshold=999999999))
        result = client.instant_query("test_query", label_key="host")

        assert len(result.series) == 0

"""Resilient Prometheus HTTP API client."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import requests
from oslo_config import cfg
from oslo_log import log as logging
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from kronos.common.exceptions import (
    PrometheusQueryError,
    PrometheusUnreachableError,
)

LOG = logging.getLogger(__name__)


class PrometheusHealth(enum.Enum):
    """Health status of the Prometheus data source."""

    HEALTHY = "healthy"
    STALE = "stale"
    PARTIAL = "partial"
    UNREACHABLE = "unreachable"


@dataclass
class QueryResult:
    """Result of a PromQL instant query."""

    query: str
    timestamp: datetime
    health: PrometheusHealth
    series: dict[str, float] = field(default_factory=dict)
    missing_labels: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_trustworthy(self) -> bool:
        return self.health == PrometheusHealth.HEALTHY


class PrometheusClient:
    """Resilient Prometheus HTTP API client.

    Queries Prometheus via ``/api/v1/query``, detects staleness and
    partial data, and retries on transient failures.
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        prom = conf.prometheus
        self._base_url = prom.url.rstrip("/")
        self._timeout = prom.timeout
        self._max_retries = prom.max_retries
        self._retry_backoff = prom.retry_backoff
        self._staleness_threshold = prom.staleness_threshold
        self._verify_ssl = prom.verify_ssl
        self._ca_cert = prom.ca_cert

        self._session = requests.Session()
        if not self._verify_ssl:
            self._session.verify = False
        elif self._ca_cert:
            self._session.verify = self._ca_cert

        token = self._resolve_bearer_token(prom)
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _resolve_bearer_token(prom: cfg.ConfigOpts) -> str | None:
        if prom.bearer_token:
            return str(prom.bearer_token)
        if prom.bearer_token_file:
            try:
                return open(prom.bearer_token_file).read().strip()
            except OSError as exc:
                LOG.warning(
                    "Failed to read bearer token file %s: %s",
                    prom.bearer_token_file,
                    exc,
                )
        return None

    def health_check(self) -> PrometheusHealth:
        """Check Prometheus reachability."""
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v1/status/runtimeinfo",
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return PrometheusHealth.HEALTHY
        except requests.ConnectionError:
            return PrometheusHealth.UNREACHABLE
        except requests.RequestException:
            return PrometheusHealth.UNREACHABLE

    def instant_query(
        self,
        query: str,
        label_key: str = "host",
        expected_labels: set[str] | None = None,
    ) -> QueryResult:
        """Execute a PromQL instant query.

        :param query: PromQL expression.
        :param label_key: Which label to use as the dict key in results.
        :param expected_labels: If provided, check for partial data.
        :returns: QueryResult with series data and health status.
        :raises PrometheusUnreachableError: If Prometheus cannot be reached.
        :raises PrometheusQueryError: If the query returns an error.
        :raises PrometheusStalenessError: If samples are older than threshold.
        :raises PrometheusPartialDataError: If expected labels are missing.
        """
        data = self._query_with_retry(query)

        result_type = data.get("resultType")
        results = data.get("result", [])

        if result_type != "vector":
            raise PrometheusQueryError(
                reason=f"Expected vector result, got '{result_type}' for query: {query}"
            )

        now = time.time()
        series: dict[str, float] = {}
        stale_samples: list[str] = []

        for item in results:
            metric = item.get("metric", {})
            label_value = metric.get(label_key)
            if label_value is None:
                continue

            timestamp, value_str = item.get("value", [0, "0"])
            try:
                value = float(value_str)
            except (ValueError, TypeError):
                LOG.warning(
                    "Non-numeric value '%s' for label %s=%s in query: %s",
                    value_str,
                    label_key,
                    label_value,
                    query,
                )
                continue

            series[label_value] = value

            sample_age = now - float(timestamp)
            if sample_age > self._staleness_threshold:
                stale_samples.append(label_value)

        # Determine health
        health = PrometheusHealth.HEALTHY
        warnings: list[str] = []
        missing: set[str] = set()

        if stale_samples:
            health = PrometheusHealth.STALE
            warnings.append(
                f"Stale samples from: {', '.join(sorted(stale_samples))}"
            )

        if expected_labels is not None:
            missing = expected_labels - series.keys()
            if missing:
                health = PrometheusHealth.PARTIAL
                warnings.append(
                    f"Missing labels: {', '.join(sorted(missing))}"
                )

        result = QueryResult(
            query=query,
            timestamp=datetime.now(tz=UTC),
            health=health,
            series=series,
            missing_labels=missing,
            warnings=warnings,
        )

        if warnings:
            LOG.warning(
                "Query health=%s for '%s': %s",
                health.value,
                query[:80],
                "; ".join(warnings),
            )

        return result

    def _query_with_retry(self, query: str) -> dict[str, Any]:
        """Execute query with retry logic."""

        @retry(  # type: ignore[untyped-decorator]
            retry=retry_if_exception_type(requests.ConnectionError),
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(multiplier=self._retry_backoff, max=30),
            reraise=True,
        )
        def _do_query() -> dict[str, Any]:
            return self._raw_query(query)

        try:
            result: dict[str, Any] = _do_query()
            return result
        except requests.ConnectionError as exc:
            raise PrometheusUnreachableError(
                url=self._base_url,
                reason=str(exc),
            ) from exc

    def _raw_query(self, query: str) -> dict[str, Any]:
        """Execute a single PromQL instant query against the HTTP API."""
        url = f"{self._base_url}/api/v1/query"
        LOG.debug("Prometheus query: %s", query[:200])

        try:
            resp = self._session.get(
                url,
                params={"query": query},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.ConnectionError:
            raise
        except requests.Timeout as exc:
            raise PrometheusUnreachableError(
                url=self._base_url,
                reason=f"Timeout after {self._timeout}s",
            ) from exc
        except requests.HTTPError as exc:
            raise PrometheusQueryError(
                reason=f"HTTP {resp.status_code}: {resp.text[:200]}"
            ) from exc

        body = resp.json()

        if body.get("status") != "success":
            error_msg = body.get("error", "unknown error")
            raise PrometheusQueryError(reason=error_msg)

        data: dict[str, Any] = body.get("data", {})
        return data

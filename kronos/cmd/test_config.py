"""Entry point for ``kronos-test-config`` validator."""

from __future__ import annotations

import sys

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient, PrometheusHealth
from kronos.common.config import register_opts
from kronos.common.exceptions import KronosException
from kronos.policies.loader import load_policies

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

# Exit codes
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_PROMETHEUS_ERROR = 2
EXIT_NOVA_ERROR = 3
EXIT_AGGREGATE_ERROR = 4


def _print_ok(msg: str) -> None:
    print(f"  \u2713 {msg}")


def _print_fail(msg: str) -> None:
    print(f"  \u2717 {msg}")


def main() -> int:
    """Validate config files and test connections."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-test-config",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-test-config")

    print("=== Kronos Config Validation ===\n")

    # 1. Load policies YAML
    print("--- Policy File ---")
    try:
        policies = load_policies(CONF.engine.policies_file)
        _print_ok(
            f"Loaded {len(policies.policies)} policies from {CONF.engine.policies_file}"
        )
        for p in policies.policies:
            status = "enabled" if p.enabled else "disabled"
            print(f"      {p.name} [{p.mode.value}] aggregate={p.aggregate} ({status})")
    except KronosException as exc:
        _print_fail(f"Policy file error: {exc}")
        return EXIT_CONFIG_ERROR

    # 2. Test Prometheus
    print("\n--- Prometheus ---")
    try:
        prom = PrometheusClient(CONF)
        health = prom.health_check()
        if health == PrometheusHealth.HEALTHY:
            _print_ok(f"Prometheus reachable at {CONF.prometheus.url}")
        else:
            _print_fail(f"Prometheus health: {health.value}")
            return EXIT_PROMETHEUS_ERROR
    except KronosException as exc:
        _print_fail(f"Prometheus error: {exc}")
        return EXIT_PROMETHEUS_ERROR

    # 3. Test Nova
    print("\n--- Nova / OpenStack ---")
    try:
        nova = NovaClient(CONF)
        nova.verify_connection()
        _print_ok("OpenStack authentication successful")
    except KronosException as exc:
        _print_fail(f"Nova error: {exc}")
        return EXIT_NOVA_ERROR

    # 4. Check aggregates
    print("\n--- Aggregates ---")
    aggregates_ok = True
    for p in policies.policies:
        if not p.enabled:
            continue
        try:
            hosts = nova.get_aggregate_hosts(p.aggregate)
            _print_ok(f"Aggregate '{p.aggregate}': {len(hosts)} hosts")
        except KronosException as exc:
            _print_fail(f"Aggregate '{p.aggregate}': {exc}")
            aggregates_ok = False

    if not aggregates_ok:
        return EXIT_AGGREGATE_ERROR

    # 5. Test PromQL queries
    print("\n--- PromQL Queries ---")
    for p in policies.policies:
        if not p.enabled:
            continue
        try:
            result = prom.instant_query(
                query=p.imbalance_query,
                label_key=p.host_label,
            )
            _print_ok(
                f"Policy '{p.name}' imbalance_query: "
                f"{len(result.series)} series returned (health={result.health.value})"
            )
            for host, score in sorted(result.series.items()):
                print(f"      {host}: {score:.3f}")
        except KronosException as exc:
            _print_fail(f"Policy '{p.name}' query error: {exc}")

    print("\n=== Overall: PASSED ===")
    return EXIT_OK

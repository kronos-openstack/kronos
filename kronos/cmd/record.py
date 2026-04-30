"""Entry point for ``kronos-record``: capture live cluster state for replay.

Connects to a real OpenStack cluster and Prometheus instance, executes
every query defined in the policies file, and writes all responses to
a snapshot directory.  The snapshot can later be replayed offline with
``kronos-replay`` to test planning logic against real data.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.config import register_opts
from kronos.policies.loader import load_policies
from kronos.policies.models import PoliciesConfig

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def _record_nova(
    nova: NovaClient,
    aggregate_names: list[str | None],
    snapshot_dir: Path,
) -> None:
    """Record all Nova data needed by the engine."""
    nova_dir = snapshot_dir / "nova"
    nova_dir.mkdir()

    # Resolve each aggregate (or the unassigned pool) to a host list.
    # The key in the snapshot is the aggregate name, or the special
    # marker for the unassigned pool.
    from kronos.common.messaging import UNASSIGNED_TOPIC_MARKER

    aggregates: dict[str, list[str]] = {}
    for name in aggregate_names:
        key = name if name is not None else UNASSIGNED_TOPIC_MARKER
        try:
            hosts = nova.get_hosts_in_aggregate(name)
            aggregates[key] = hosts
            LOG.info("Recorded aggregate '%s': %d hosts", key, len(hosts))
        except Exception:
            LOG.error("Failed to fetch aggregate '%s'", key, exc_info=True)
            aggregates[key] = []

    (nova_dir / "aggregates.json").write_text(
        json.dumps(aggregates, indent=2),
    )

    # Instances: for each unique host across all aggregates
    all_hosts = {h for hosts in aggregates.values() for h in hosts}
    instances: dict[str, list[dict[str, object]]] = {}
    for host in sorted(all_hosts):
        try:
            host_instances = nova.list_instances_on_host(host)
            instances[host] = [
                dataclasses.asdict(inst) for inst in host_instances
            ]
            LOG.info(
                "Recorded %d instances on host '%s'",
                len(host_instances),
                host,
            )
        except Exception:
            LOG.error(
                "Failed to list instances on '%s'",
                host,
                exc_info=True,
            )
            instances[host] = []

    (nova_dir / "instances.json").write_text(
        json.dumps(instances, indent=2),
    )

    # Server groups
    try:
        groups = nova.list_server_groups()
        (nova_dir / "server_groups.json").write_text(
            json.dumps(groups, indent=2),
        )
        LOG.info("Recorded %d server groups", len(groups))
    except Exception:
        LOG.error("Failed to list server groups", exc_info=True)
        (nova_dir / "server_groups.json").write_text("[]")


def _record_prometheus(
    prometheus: PrometheusClient,
    policies: PoliciesConfig,
    snapshot_dir: Path,
) -> None:
    """Record all Prometheus query results needed by the engine."""
    prom_dir = snapshot_dir / "prometheus"
    prom_dir.mkdir()

    for policy in policies.policies:
        if not policy.enabled:
            continue

        # Imbalance query
        try:
            result = prometheus.instant_query(
                query=policy.imbalance_query,
                label_key=policy.host_label,
            )
            (prom_dir / f"imbalance_{policy.name}.json").write_text(
                json.dumps(
                    {
                        "query": result.query,
                        "health": result.health.value,
                        "series": result.series,
                        "missing_labels": sorted(result.missing_labels),
                        "warnings": result.warnings,
                    },
                    indent=2,
                ),
            )
            LOG.info(
                "Recorded imbalance query for '%s': %d series",
                policy.name,
                len(result.series),
            )
        except Exception:
            LOG.error(
                "Failed imbalance query for '%s'",
                policy.name,
                exc_info=True,
            )

        # VM profile query (optional)
        if policy.vm_profile_query:
            try:
                result = prometheus.instant_query(
                    query=policy.vm_profile_query,
                    label_key=policy.vm_profile_label,
                )
                (prom_dir / f"vm_profile_{policy.name}.json").write_text(
                    json.dumps(
                        {
                            "query": result.query,
                            "health": result.health.value,
                            "series": result.series,
                            "missing_labels": sorted(result.missing_labels),
                            "warnings": result.warnings,
                        },
                        indent=2,
                    ),
                )
                LOG.info(
                    "Recorded vm_profile query for '%s': %d series",
                    policy.name,
                    len(result.series),
                )
            except Exception:
                LOG.error(
                    "Failed vm_profile query for '%s'",
                    policy.name,
                    exc_info=True,
                )


def main() -> int:
    """Main entry point for kronos-record."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF.register_cli_opt(
        cfg.StrOpt(
            "output-dir",
            positional=True,
            help="Directory to write the snapshot to.",
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-record",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-record")

    output_dir = Path(CONF["output_dir"])
    if output_dir.exists():
        LOG.error("Output directory already exists: %s", output_dir)
        return 1

    policies = load_policies(CONF.engine.policies_file)
    aggregate_names: list[str | None] = list(CONF.engine.aggregates)
    if CONF.engine.include_unassigned_hosts:
        aggregate_names.append(None)

    if not aggregate_names:
        LOG.error(
            "No aggregates to record. Set [engine] aggregates or "
            "[engine] include_unassigned_hosts in kronos.conf.",
        )
        return 1

    LOG.info(
        "Recording snapshot for %d policies across %d aggregates to %s",
        len(policies.policies),
        len(aggregate_names),
        output_dir,
    )

    output_dir.mkdir(parents=True)

    # Write metadata
    (output_dir / "meta.json").write_text(
        json.dumps(
            {
                "recorded_at": datetime.now(tz=UTC).isoformat(),
                "policies_file": CONF.engine.policies_file,
                "policy_names": [p.name for p in policies.policies],
                "aggregates": [
                    a if a is not None else "_unassigned_"
                    for a in aggregate_names
                ],
            },
            indent=2,
        ),
    )

    nova = NovaClient(CONF)
    prometheus = PrometheusClient(CONF)

    _record_nova(nova, aggregate_names, output_dir)
    _record_prometheus(prometheus, policies, output_dir)

    # Empty cooldown template - operators edit this to reproduce
    # specific cooldown / quarantine scenarios in kronos-replay.
    (output_dir / "cooldowns.json").write_text(
        json.dumps(
            {
                "aggregate_cooldowns": {},
                "instance_cooldowns": {},
                "instance_quarantines": {},
            },
            indent=2,
        ) + "\n",
    )

    LOG.info("Snapshot written to %s", output_dir)
    return 0

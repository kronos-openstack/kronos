"""Shared snapshot writer used by ``kronos-record`` and the engine.

Both entry points capture the same Nova + Prometheus state needed by
``kronos-replay`` so they call into a single set of helpers. The
on-disk layout is:

    <output_dir>/
      meta.json
      cooldowns.json
      nova/
        aggregates.json
        instances.json
        server_groups.json
        services.json
      prometheus/
        imbalance_<policy>.json
        vm_profile_<policy>.json
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.messaging import UNASSIGNED_TOPIC_MARKER
from kronos.policies.models import PoliciesConfig

LOG = logging.getLogger(__name__)


SNAPSHOT_SUBDIR_PREFIX = "kronos-engine-snapshot-"


def write_snapshot(
    parent_dir: Path,
    nova: NovaClient,
    prometheus: PrometheusClient,
    policies: PoliciesConfig,
    aggregate_names: list[str | None],
) -> Path:
    """Write a complete snapshot under ``parent_dir`` and return its path.

    ``write_snapshot`` always creates a fresh timestamped subdirectory
    of the form ``kronos-engine-snapshot-<UTC>`` inside ``parent_dir``
    and populates that.  Both ``kronos-record`` and the engine's
    SIGUSR1 handler call this so every snapshot has a unique,
    self-describing name.

    ``parent_dir`` and any missing ancestors are created if needed.
    Per-section failures are logged and produce empty files rather
    than aborting the whole snapshot, matching the behaviour of the
    original ``kronos-record`` CLI.
    """
    parent_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    target = parent_dir / f"{SNAPSHOT_SUBDIR_PREFIX}{timestamp}"
    target.mkdir()

    (target / "meta.json").write_text(
        json.dumps(
            {
                "recorded_at": datetime.now(tz=UTC).isoformat(),
                "policy_names": [p.name for p in policies.policies],
                "aggregates": [
                    a if a is not None else UNASSIGNED_TOPIC_MARKER
                    for a in aggregate_names
                ],
            },
            indent=2,
        ),
    )

    _write_nova(nova, aggregate_names, target)
    _write_prometheus(prometheus, policies, target)

    (target / "cooldowns.json").write_text(
        json.dumps(
            {
                "aggregate_cooldowns": {},
                "instance_cooldowns": {},
                "instance_quarantines": {},
            },
            indent=2,
        ) + "\n",
    )

    return target


def _write_nova(
    nova: NovaClient,
    aggregate_names: list[str | None],
    output_dir: Path,
) -> None:
    nova_dir = output_dir / "nova"
    nova_dir.mkdir()

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
                "Failed to list instances on '%s'", host, exc_info=True,
            )
            instances[host] = []

    (nova_dir / "instances.json").write_text(
        json.dumps(instances, indent=2),
    )

    try:
        groups = nova.list_server_groups()
        (nova_dir / "server_groups.json").write_text(
            json.dumps(groups, indent=2),
        )
        LOG.info("Recorded %d server groups", len(groups))
    except Exception:
        LOG.error("Failed to list server groups", exc_info=True)
        (nova_dir / "server_groups.json").write_text("[]")

    try:
        services = nova.list_compute_services()
        (nova_dir / "services.json").write_text(
            json.dumps(
                [dataclasses.asdict(svc) for svc in services],
                indent=2,
            ),
        )
        LOG.info("Recorded %d nova-compute services", len(services))
    except Exception:
        LOG.error("Failed to list compute services", exc_info=True)
        (nova_dir / "services.json").write_text("[]")


def _write_prometheus(
    prometheus: PrometheusClient,
    policies: PoliciesConfig,
    output_dir: Path,
) -> None:
    prom_dir = output_dir / "prometheus"
    prom_dir.mkdir()

    for policy in policies.policies:
        if not policy.enabled:
            continue

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

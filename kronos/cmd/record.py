"""Entry point for ``kronos-record``: capture live cluster state for replay.

Connects to a real OpenStack cluster and Prometheus instance, executes
every query defined in the policies file, and writes all responses to
a snapshot directory.  The snapshot can later be replayed offline with
``kronos-replay`` to test planning logic against real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

from oslo_config import cfg
from oslo_log import log as logging

from kronos.clients.nova import NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.common.config import register_opts
from kronos.common.snapshot import write_snapshot
from kronos.policies.loader import load_policies

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def main() -> int:
    """Main entry point for kronos-record."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF.register_cli_opt(
        cfg.StrOpt(
            "output-dir",
            positional=True,
            help=(
                "Parent directory under which the snapshot is written. "
                "A fresh timestamped subdirectory is created inside it; "
                "the printed path is what you pass to kronos-replay."
            ),
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-record",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-record")

    parent_dir = Path(CONF["output_dir"])

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
        "Recording snapshot for %d policies across %d aggregates under %s",
        len(policies.policies),
        len(aggregate_names),
        parent_dir,
    )

    nova = NovaClient(CONF)
    prometheus = PrometheusClient(CONF)
    target = write_snapshot(
        parent_dir, nova, prometheus, policies, aggregate_names,
    )

    LOG.info("Snapshot written to %s", target)
    return 0

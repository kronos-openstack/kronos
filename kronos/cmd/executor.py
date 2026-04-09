"""Entry point for ``kronos-executor``: migration execution daemon.

Consumes migration tasks for one aggregate from RabbitMQ and executes
them via the Nova live-migrate API.  One executor process per aggregate
(or one for the unassigned-hosts pool).

Usage::

    kronos-executor --config-file /etc/kronos/kronos.conf \\
                    --aggregate gpu-aggregate

    kronos-executor --config-file /etc/kronos/kronos.conf \\
                    --unassigned
"""

from __future__ import annotations

import signal
import sys
from types import FrameType

from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import register_opts
from kronos.executor.worker import ExecutorWorker

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def main() -> int:
    """Main entry point for kronos-executor."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF.register_cli_opt(
        cfg.StrOpt(
            "aggregate",
            default=None,
            help="Nova host aggregate this executor handles.",
        ),
    )
    CONF.register_cli_opt(
        cfg.BoolOpt(
            "unassigned",
            default=False,
            help="Handle the pool of hosts that are not members of any aggregate.",
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-executor",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-executor")

    if CONF.aggregate is None and not CONF.unassigned:
        LOG.error(
            "kronos-executor requires either --aggregate NAME or --unassigned.",
        )
        return 1
    if CONF.aggregate is not None and CONF.unassigned:
        LOG.error(
            "kronos-executor: --aggregate and --unassigned are mutually exclusive.",
        )
        return 1

    aggregate: str | None = None if CONF.unassigned else CONF.aggregate
    label = aggregate if aggregate is not None else "<unassigned>"
    LOG.info("kronos-executor starting for aggregate '%s'", label)

    worker = ExecutorWorker(CONF, aggregate)

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down...", sig_name)
        worker.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker.start()
    return 0

"""Entry point for ``kronos-executor``: migration execution daemon.

Consumes migration tasks from the per-aggregate RabbitMQ topic and
executes them via the Nova live-migrate API.

Usage::

    kronos-executor --config-file /etc/kronos/kronos.conf \\
                    --aggregate gpu-aggregate
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
            required=True,
            help="Nova host aggregate this executor handles.",
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-executor",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-executor")

    aggregate = CONF.aggregate
    LOG.info("kronos-executor starting for aggregate '%s'", aggregate)

    worker = ExecutorWorker(CONF, aggregate)

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down...", sig_name)
        worker.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker.start()
    return 0

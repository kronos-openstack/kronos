"""Entry point for ``kronos-engine`` daemon."""

from __future__ import annotations

import sys

from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import register_opts
from kronos.engine.loop import EngineLoop

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def main() -> int:
    """Main entry point for kronos-engine."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-engine",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-engine")

    LOG.info("Starting kronos-engine")

    engine = EngineLoop(CONF)
    engine.start()
    return 0

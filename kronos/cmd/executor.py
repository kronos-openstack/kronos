"""Entry point for ``kronos-executor``: migration execution daemon.

Consumes migration tasks from RabbitMQ and executes them via the Nova
live-migrate API.  One executor process services one or more aggregates
(and/or the unassigned-hosts pool); each aggregate runs as an independent
unit on its own threads.

Usage::

    kronos-executor --config-file /etc/kronos/kronos.conf \\
                    --aggregate gpu-aggregate

    # One process servicing several aggregates plus the unassigned pool:
    kronos-executor --config-file /etc/kronos/kronos.conf \\
                    --aggregate gpu-aggregate \\
                    --aggregate hpc-aggregate \\
                    --unassigned

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


def resolve_scopes(
    aggregates: list[str],
    unassigned: bool,
) -> list[str | None]:
    """Build the ordered list of executor scopes from CLI options.

    Named aggregates come first in the order given, followed by the
    unassigned-hosts pool (represented as ``None``) when requested.
    Duplicate aggregate names are dropped with a warning so a typo like
    ``--aggregate gpu --aggregate gpu`` does not start two competing
    consumers in one process.

    :raises ValueError: when no scope is selected at all.
    """
    scopes: list[str | None] = []
    seen: set[str] = set()
    for name in aggregates:
        if name in seen:
            LOG.warning(
                "Aggregate '%s' specified more than once; "
                "ignoring the duplicate.",
                name,
            )
            continue
        seen.add(name)
        scopes.append(name)
    if unassigned:
        scopes.append(None)
    if not scopes:
        raise ValueError(
            "kronos-executor requires at least one --aggregate NAME "
            "(repeatable) and/or --unassigned.",
        )
    return scopes


def main() -> int:
    """Main entry point for kronos-executor."""
    logging.register_options(CONF)
    register_opts(CONF)

    CONF.register_cli_opt(
        cfg.MultiStrOpt(
            "aggregate",
            default=[],
            help=(
                "Nova host aggregate this executor handles.  Repeat the "
                "flag to service several aggregates from one process."
            ),
        ),
    )
    CONF.register_cli_opt(
        cfg.BoolOpt(
            "unassigned",
            default=False,
            help=(
                "Also handle the pool of hosts that are not members of any "
                "aggregate.  May be combined with one or more --aggregate."
            ),
        ),
    )

    CONF(
        sys.argv[1:],
        project="kronos",
        prog="kronos-executor",
        default_config_files=["/etc/kronos/kronos.conf"],
    )
    logging.setup(CONF, "kronos-executor")

    try:
        scopes = resolve_scopes(list(CONF.aggregate), CONF.unassigned)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1

    labels = ", ".join(s if s is not None else "<unassigned>" for s in scopes)
    LOG.info("kronos-executor starting for aggregates: %s", labels)

    worker = ExecutorWorker(CONF, scopes)

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        # Signal handlers must do only async-signal-safe work.  Just
        # nudge the worker; the main thread runs the actual shutdown
        # (oslo.messaging takes locks that aren't safe to acquire here,
        # and a re-entrant signal during stop() can deadlock).
        sig_name = signal.Signals(signum).name
        LOG.info("Received %s, shutting down...", sig_name)
        worker.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker.start()
    return 0

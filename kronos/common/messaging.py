"""oslo.messaging transport, RPC, and notifier helpers.

Two message flows:

- **Engine → Executor**: RPC cast (exactly-one delivery, competing consumers
  semantics). Work queue — we want one executor to pick up each migration
  task, not broadcast.
- **Executor → Engine(s)**: Notifications (broadcast, multiple listeners).
  Both active and passive engines subscribe to keep cooldown state warm.

Two transports because oslo.messaging uses separate transport instances
for RPC vs notifications.  They read the same ``transport_url``, but
notifications can override via ``[oslo_messaging_notifications]
transport_url`` if desired.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import MESSAGING_GROUP

LOG = logging.getLogger(__name__)


def get_rpc_transport(conf: cfg.ConfigOpts) -> oslo_messaging.Transport:
    """Create an oslo.messaging RPC transport (for engine→executor)."""
    url = conf[MESSAGING_GROUP].transport_url
    return oslo_messaging.get_rpc_transport(conf, url=url)


def get_notification_transport(
    conf: cfg.ConfigOpts,
) -> oslo_messaging.Transport:
    """Create an oslo.messaging notification transport (for executor→engine)."""
    url = conf[MESSAGING_GROUP].transport_url
    return oslo_messaging.get_notification_transport(conf, url=url)


# --- RPC (engine → executor) ---


# Topic component used when an aggregate is the unassigned-hosts pool.
# Picked to be invalid as a Nova aggregate name (dashes/colons aren't in
# the aggregate name charset, and the leading underscore is unusual).
UNASSIGNED_TOPIC_MARKER = "_unassigned_"


def _topic_component(aggregate: str | None) -> str:
    """Return the topic-safe identifier for an aggregate or the unassigned pool."""
    return aggregate if aggregate is not None else UNASSIGNED_TOPIC_MARKER


def migrations_topic(aggregate: str | None) -> str:
    """Return the RPC topic for migration tasks.

    >>> migrations_topic("gpu-aggregate")
    'kronos.migrations.gpu-aggregate'
    >>> migrations_topic(None)
    'kronos.migrations._unassigned_'
    """
    return f"kronos.migrations.{_topic_component(aggregate)}"


def get_rpc_client(
    transport: oslo_messaging.Transport,
    aggregate: str | None,
) -> oslo_messaging.RPCClient:
    """Create an RPC client that casts migration tasks to an aggregate topic.

    Pass ``None`` for the unassigned-hosts pool.  Uses ``cast()`` for
    fire-and-forget delivery — the engine never waits for a response.
    Exactly-one delivery via competing consumers.
    """
    target = oslo_messaging.Target(
        topic=migrations_topic(aggregate),
        version="1.0",
    )
    return oslo_messaging.get_rpc_client(transport, target)


def get_rpc_server(
    transport: oslo_messaging.Transport,
    aggregate: str | None,
    endpoints: list[object],
) -> oslo_messaging.MessageHandlingServer:
    """Create an RPC server that processes migration tasks for an aggregate.

    Pass ``None`` for the unassigned-hosts pool.  One server per topic;
    competing consumers share a single queue.
    """
    target = oslo_messaging.Target(
        topic=migrations_topic(aggregate),
        server=f"kronos-executor-{_topic_component(aggregate)}",
        version="1.0",
    )
    return oslo_messaging.get_rpc_server(
        transport,
        target,
        endpoints,
        executor="threading",
    )


# --- Notifications (executor → engine) ---


def results_topic(aggregate: str | None) -> str:
    """Return the notification topic for migration results.

    >>> results_topic("gpu-aggregate")
    'kronos.results.gpu-aggregate'
    >>> results_topic(None)
    'kronos.results._unassigned_'
    """
    return f"kronos.results.{_topic_component(aggregate)}"


def get_notifier(
    transport: oslo_messaging.Transport,
    topic: str,
    publisher_id: str,
) -> oslo_messaging.Notifier:
    """Create a Notifier for publishing migration results.

    The ``messagingv2`` driver is selected explicitly.  Without it, Notifier
    falls back to the ``noop`` driver and silently discards every call.
    """
    return oslo_messaging.Notifier(
        transport,
        publisher_id=publisher_id,
        driver="messagingv2",
        topics=[topic],
    )


def get_notification_listener(
    transport: oslo_messaging.Transport,
    topic: str,
    endpoints: list[object],
) -> oslo_messaging.MessageHandlingServer:
    """Create a notification listener for an aggregate's results topic."""
    target = oslo_messaging.Target(topic=topic)
    return oslo_messaging.get_notification_listener(
        transport,
        [target],
        endpoints,
        executor="threading",
    )


# --- Serialization helpers (shared) ---


def task_to_dict(task: object) -> dict[str, object]:
    """Serialize a dataclass to a dict suitable for oslo.messaging payload.

    Converts datetime fields to ISO 8601 strings.
    """
    d: dict[str, object] = {}
    for f in dataclasses.fields(task):
        value = getattr(task, f.name)
        if isinstance(value, datetime):
            d[f.name] = value.isoformat()
        else:
            d[f.name] = value
    return d


def dict_to_dataclass(cls: type, data: dict[str, object]) -> object:
    """Deserialize a dict (from oslo.messaging payload) into a dataclass.

    Handles datetime fields by parsing ISO 8601 strings.
    """
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(cls):
        value = data.get(f.name)
        if value is not None:
            kwargs[f.name] = value
        elif f.default is not dataclasses.MISSING:
            kwargs[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            kwargs[f.name] = f.default_factory()
    return cls(**kwargs)

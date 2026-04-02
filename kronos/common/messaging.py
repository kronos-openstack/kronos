"""oslo.messaging transport and notifier helpers.

Provides factory functions for creating oslo.messaging transports and
notifiers, plus serialization helpers for MigrationTask/MigrationResult
dataclasses.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import oslo_messaging
from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import MESSAGING_GROUP

LOG = logging.getLogger(__name__)


def get_transport(conf: cfg.ConfigOpts) -> oslo_messaging.Transport:
    """Create an oslo.messaging notification transport.

    :param conf: oslo.config with ``[messaging]`` group registered.
    :returns: Transport instance.
    :raises: oslo_messaging.TransportDriverError if transport_url is invalid.
    """
    url = conf[MESSAGING_GROUP].transport_url
    return oslo_messaging.get_notification_transport(conf, url=url)


def get_notifier(
    transport: oslo_messaging.Transport,
    topic: str,
    publisher_id: str | None = None,
) -> oslo_messaging.Notifier:
    """Create an oslo.messaging Notifier for a specific topic.

    :param transport: oslo.messaging transport.
    :param topic: Notification topic (e.g. ``kronos.migrations.gpu-aggregate``).
    :param publisher_id: Publisher identifier. Defaults to ``kronos-engine``.
    :returns: Notifier instance.
    """
    return oslo_messaging.Notifier(
        transport,
        publisher_id=publisher_id or "kronos-engine",
        topics=[topic],
    )


def get_listener(
    transport: oslo_messaging.Transport,
    targets: list[oslo_messaging.Target],
    endpoints: list[object],
) -> oslo_messaging.NotificationListenerBase:
    """Create an oslo.messaging notification listener.

    :param transport: oslo.messaging transport.
    :param targets: List of (topic, exchange) targets to subscribe to.
    :param endpoints: List of endpoint objects with handler methods.
    :returns: NotificationListener instance (call ``.start()`` to begin consuming).
    """
    return oslo_messaging.get_notification_listener(
        transport,
        targets,
        endpoints,
        executor="threading",
    )


def migrations_topic(aggregate: str) -> str:
    """Return the migration task topic for an aggregate.

    >>> migrations_topic("gpu-aggregate")
    'kronos.migrations.gpu-aggregate'
    """
    return f"kronos.migrations.{aggregate}"


def results_topic(aggregate: str) -> str:
    """Return the migration results topic for an aggregate.

    >>> results_topic("gpu-aggregate")
    'kronos.results.gpu-aggregate'
    """
    return f"kronos.results.{aggregate}"


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


def emit_notification(
    notifier: oslo_messaging.Notifier,
    event_type: str,
    payload: dict[str, object],
) -> None:
    """Publish a notification at INFO priority.

    :param notifier: oslo.messaging Notifier.
    :param event_type: Event type string (e.g. ``migration.requested``).
    :param payload: Serialized payload dict.
    """
    notifier.info({}, event_type, payload)
    LOG.debug("Published %s: %s", event_type, payload.get("task_id", ""))

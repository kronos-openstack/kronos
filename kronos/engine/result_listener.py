"""Notification endpoint for migration results on ``kronos.results.*``.

The executor publishes ``migration.completed`` and ``migration.failed``
events after every attempt.  This endpoint consumes them and updates
engine state per the agreed matrix:

- ``migration.completed``: debug log only, no state change.
- ``migration.failed`` with retries still pending: debug log only.
- ``migration.failed`` at the last attempt: info log, and for
  PreFlightError / MigrationFailed / MigrationTimeout the instance is
  quarantined for ``instance_quarantine_seconds``.  NovaClientError is
  treated as transient - no quarantine, the regular instance cooldown
  governs when the VM may be re-planned.
"""

from __future__ import annotations

from typing import cast

from oslo_log import log as logging

from kronos.engine.cooldown import CooldownTracker

LOG = logging.getLogger(__name__)

# Exception class names that indicate a VM-specific problem and warrant
# quarantine on the final attempt.
QUARANTINE_ERROR_TYPES = frozenset(
    {"PreFlightError", "MigrationFailed", "MigrationTimeout"},
)
# Transient infrastructure errors - no quarantine, just let the normal
# instance cooldown take effect.  PlacementRejected lives here because a
# capacity shortfall on the destination is expected to resolve on its
# own once other moves complete or VMs free their claims; quarantining
# the VM would just pin a healthy workload to a hot host.
TRANSIENT_ERROR_TYPES = frozenset({"NovaClientError", "PlacementRejected"})


class MigrationResultEndpoint:
    """oslo.messaging notification endpoint for migration results.

    oslo.messaging dispatches notifications to ``info()`` (priority
    level).  The endpoint switches on ``event_type`` to split
    completed vs. failed.
    """

    def __init__(
        self,
        cooldown: CooldownTracker,
        max_retries: int,
        quarantine_seconds: int,
    ) -> None:
        self._cooldown = cooldown
        self._max_retries = max_retries
        self._quarantine_seconds = quarantine_seconds

    def info(
        self,
        ctxt: dict[str, object],
        publisher_id: str,
        event_type: str,
        payload: dict[str, object],
        metadata: dict[str, object],
    ) -> None:
        if event_type == "migration.completed":
            self._on_completed(payload)
        elif event_type == "migration.failed":
            self._on_failed(payload)
        else:
            LOG.debug(
                "Ignoring unknown event '%s' from %s",
                event_type,
                publisher_id,
            )

    def _on_completed(self, payload: dict[str, object]) -> None:
        LOG.debug(
            "Migration completed: task=%s instance=%s %s -> %s",
            payload.get("task_id"),
            payload.get("instance_uuid"),
            payload.get("from_host"),
            payload.get("to_host"),
        )

    def _on_failed(self, payload: dict[str, object]) -> None:
        retry_count = cast(int, payload.get("retry_count") or 0)
        if retry_count < self._max_retries:
            LOG.debug(
                "Migration failed (retry pending): task=%s instance=%s "
                "attempt=%d/%d error_type=%s",
                payload.get("task_id"),
                payload.get("instance_uuid"),
                retry_count + 1,
                self._max_retries + 1,
                payload.get("error_type"),
            )
            return

        # Final attempt: retries exhausted.
        error_type = str(payload.get("error_type", ""))
        instance_uuid = str(payload.get("instance_uuid", ""))
        aggregate = payload.get("aggregate")
        error = payload.get("error")

        if error_type in QUARANTINE_ERROR_TYPES:
            LOG.info(
                "Migration gave up on instance %s in '%s' after %d attempts "
                "(%s): %s. Applying quarantine.",
                instance_uuid,
                aggregate,
                retry_count + 1,
                error_type,
                error,
            )
            self._cooldown.quarantine_instance(
                instance_uuid, self._quarantine_seconds,
            )
        elif error_type in TRANSIENT_ERROR_TYPES:
            LOG.info(
                "Migration gave up on instance %s in '%s' after %d attempts "
                "(%s): %s. Treating as transient, relying on instance "
                "cooldown before re-planning.",
                instance_uuid,
                aggregate,
                retry_count + 1,
                error_type,
                error,
            )
        else:
            LOG.info(
                "Migration gave up on instance %s in '%s' after %d attempts "
                "with unclassified error_type=%r: %s. Applying quarantine "
                "defensively.",
                instance_uuid,
                aggregate,
                retry_count + 1,
                error_type,
                error,
            )
            self._cooldown.quarantine_instance(
                instance_uuid, self._quarantine_seconds,
            )

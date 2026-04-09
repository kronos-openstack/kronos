"""Cooldown tracking for the engine.

Prevents oscillation and migration storms.  Two levels:

- **Aggregate-level**: after emitting a plan for aggregate X, skip
  planning for the same aggregate until ``engine.cooldown`` expires.
  Global across all policies — combined scoring emits one plan per
  aggregate per cycle.
- **Instance-level**: after including a VM in a plan, skip it for
  ``engine.instance_cooldown`` seconds.  Prevents a VM bouncing
  between hosts via different migrations.

Active and passive engines both maintain a tracker.  Passive engines
update from migration-result notifications so they have warm state on
failover.
"""

from __future__ import annotations

import time

from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class CooldownTracker:
    """In-memory cooldown state for one engine instance."""

    def __init__(
        self,
        aggregate_cooldown_seconds: float,
        instance_cooldown_seconds: float,
    ) -> None:
        self._aggregate_cooldown_seconds = aggregate_cooldown_seconds
        self._instance_cooldown_seconds = instance_cooldown_seconds
        # aggregate_name -> monotonic timestamp of last plan emission
        self._aggregate_emissions: dict[str, float] = {}
        # instance_uuid -> monotonic timestamp of last inclusion in a plan
        self._instance_emissions: dict[str, float] = {}

    def record_plan_emission(
        self,
        aggregate: str,
        instance_uuids: list[str],
    ) -> None:
        """Record that a plan was emitted for an aggregate."""
        now = time.monotonic()
        self._aggregate_emissions[aggregate] = now
        for instance_uuid in instance_uuids:
            self._instance_emissions[instance_uuid] = now

    def is_aggregate_cooling(self, aggregate: str) -> bool:
        """True if the aggregate is still in its cooldown window."""
        last = self._aggregate_emissions.get(aggregate)
        if last is None:
            return False
        elapsed = time.monotonic() - last
        if elapsed >= self._aggregate_cooldown_seconds:
            return False
        remaining = self._aggregate_cooldown_seconds - elapsed
        LOG.debug(
            "Aggregate '%s' still cooling down (%.0fs remaining).",
            aggregate,
            remaining,
        )
        return True

    def is_instance_cooling(self, instance_uuid: str) -> bool:
        """True if the instance is still in its cooldown window."""
        last = self._instance_emissions.get(instance_uuid)
        if last is None:
            return False
        return (time.monotonic() - last) < self._instance_cooldown_seconds

    def update_from_result(
        self,
        aggregate: str,
        instance_uuid: str,
        success: bool,
    ) -> None:
        """Update cooldown state from a migration result.

        Called by active and passive engines when a
        ``migration.completed`` or ``migration.failed`` notification
        arrives.  Keeps passive engine state warm for failover.
        """
        now = time.monotonic()
        self._aggregate_emissions[aggregate] = now
        self._instance_emissions[instance_uuid] = now

    def cleanup_expired(self, max_age_seconds: float = 3600.0) -> None:
        """Remove stale entries to prevent unbounded growth."""
        now = time.monotonic()
        self._aggregate_emissions = {
            k: v for k, v in self._aggregate_emissions.items()
            if (now - v) < max_age_seconds
        }
        self._instance_emissions = {
            k: v for k, v in self._instance_emissions.items()
            if (now - v) < max_age_seconds
        }

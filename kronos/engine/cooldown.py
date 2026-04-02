"""Cooldown tracking for the engine.

Prevents oscillation and migration storms by enforcing minimum intervals
between plan emissions.  Both active and passive engines maintain a
tracker — the passive engine updates from migration results so it has
warm state on failover.

Two levels of cooldown:

- **Policy-level**: after emitting a plan for policy X, don't emit
  another for that policy until the policy's ``cooldown`` expires.
- **Instance-level**: after including a VM in a plan, don't include
  it again until the configured ``instance_cooldown`` expires (prevents
  the same VM bouncing between hosts across different policies).
"""

from __future__ import annotations

import time
from datetime import timedelta

from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class CooldownTracker:
    """In-memory cooldown state for one engine instance.

    Thread-safe: the engine loop is single-threaded in M3, so no
    locking is needed.  If the engine becomes multi-threaded in future,
    add a threading.Lock.
    """

    def __init__(self, instance_cooldown_seconds: float) -> None:
        self._instance_cooldown_seconds = instance_cooldown_seconds
        # policy_name -> monotonic timestamp of last plan emission
        self._policy_emissions: dict[str, float] = {}
        # instance_uuid -> monotonic timestamp of last inclusion in a plan
        self._instance_emissions: dict[str, float] = {}

    def record_plan_emission(
        self,
        policy_name: str,
        instance_uuids: list[str],
    ) -> None:
        """Record that a plan was emitted for a policy.

        Called by the engine after publishing migration tasks.

        :param policy_name: The policy that was planned.
        :param instance_uuids: UUIDs of instances included in the plan.
        """
        now = time.monotonic()
        self._policy_emissions[policy_name] = now
        for instance_uuid in instance_uuids:
            self._instance_emissions[instance_uuid] = now

    def is_policy_cooling(
        self,
        policy_name: str,
        cooldown: timedelta,
    ) -> bool:
        """Check if a policy is still in its cooldown period.

        :param policy_name: The policy to check.
        :param cooldown: The policy's configured cooldown duration.
        :returns: True if the policy was recently planned and should be skipped.
        """
        last = self._policy_emissions.get(policy_name)
        if last is None:
            return False
        elapsed = time.monotonic() - last
        if elapsed >= cooldown.total_seconds():
            return False
        remaining = cooldown.total_seconds() - elapsed
        LOG.debug(
            "Policy '%s' still cooling down (%.0fs remaining).",
            policy_name,
            remaining,
        )
        return True

    def is_instance_cooling(self, instance_uuid: str) -> bool:
        """Check if an instance is still in its cooldown period.

        :param instance_uuid: The instance UUID to check.
        :returns: True if the instance was recently planned and should be skipped.
        """
        last = self._instance_emissions.get(instance_uuid)
        if last is None:
            return False
        return (time.monotonic() - last) < self._instance_cooldown_seconds

    def update_from_result(
        self,
        policy_name: str,
        instance_uuid: str,
        success: bool,
    ) -> None:
        """Update cooldown state from a migration result.

        Called by both active and passive engines when they receive
        a ``migration.completed`` or ``migration.failed`` event.
        This keeps the passive engine's cooldown state warm for failover.

        On failure we still maintain the cooldown — we don't want to
        immediately retry a failed migration.

        :param policy_name: Policy that triggered the migration.
        :param instance_uuid: Instance that was migrated.
        :param success: Whether the migration succeeded.
        """
        now = time.monotonic()
        self._policy_emissions[policy_name] = now
        self._instance_emissions[instance_uuid] = now

    def cleanup_expired(self, max_age_seconds: float = 3600.0) -> None:
        """Remove stale entries older than ``max_age_seconds``.

        Called periodically to prevent unbounded memory growth.
        """
        now = time.monotonic()
        self._policy_emissions = {
            k: v for k, v in self._policy_emissions.items()
            if (now - v) < max_age_seconds
        }
        self._instance_emissions = {
            k: v for k, v in self._instance_emissions.items()
            if (now - v) < max_age_seconds
        }

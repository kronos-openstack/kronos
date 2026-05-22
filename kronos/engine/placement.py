"""Pluggable Nova-placement claims gate for the planner pipeline.

The Prometheus-driven scorer reasons in actual-utilization space, but
Nova's claims tracker refuses a live migration whose flavor reservation
would exceed the destination's placement inventory after
``cpu_allocation_ratio`` / ``ram_allocation_ratio`` /
``disk_allocation_ratio``.  Pack plans (and even some spread plans)
that look fine to Kronos can hit ``400 No valid host was found`` at
execute time.

The :class:`PlacementGate` closes that gap at *plan* time.  It
maintains a per-cycle, per-host headroom ledger keyed by the same
resource classes Nova claims against (``VCPU``, ``MEMORY_MB``,
``DISK_GB``) and exposes two operations:

* :meth:`is_destination_ok` - speculative check, called from the
  :class:`~kronos.engine.constraints.ConstraintChecker` for every
  candidate move regardless of mode (spread, pack, evacuator,
  affinity enforcer).
* :meth:`commit_move` - debit destination, credit source; called from
  each mover immediately after it accepts a move.  Keeps the ledger
  consistent across speculative moves within a cycle so subsequent
  candidates see a truthful picture.

The gate is *pluggable*: the constraint checker holds an optional
reference and short-circuits when no gate is installed (the default
when ``[engine] enforce_placement_claims`` is off).  Disabling the
gate restores the pre-existing planner behaviour bit-for-bit.

Failure model:

* When the gate is disabled, every check returns ``True``.
* When the gate is enabled but the ledger has no entry for a host
  (provider snapshot missing), the host fails closed - no migration
  may land there.  This mirrors the constraint checker's host
  availability gate, which fails closed when service state is
  unknown.
* When a VM has no flavor footprint installed, the gate fails open
  for that VM (planner can still move it) and logs once.  Missing
  flavor data usually means the VM was not visible at profile time -
  the planner shouldn't propose it as a candidate anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from oslo_log import log as logging

from kronos.clients.placement import (
    RC_DISK_GB,
    RC_MEMORY_MB,
    RC_VCPU,
    ProviderSnapshot,
)

LOG = logging.getLogger(__name__)


@dataclass
class FlavorFootprint:
    """Per-VM claim footprint, in the same units as the headroom ledger."""

    vcpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0

    def as_claim(self) -> dict[str, int]:
        return {
            RC_VCPU: self.vcpus,
            RC_MEMORY_MB: self.memory_mb,
            RC_DISK_GB: self.disk_gb,
        }


@dataclass
class _Headroom:
    """Mutable per-host remaining capacity for tracked resource classes.

    ``check_disk`` controls whether ``DISK_GB`` participates in
    ``fits`` / ``debit`` / ``credit``.  Disabled by default because
    Ceph-backed (RBD) ephemeral storage reports the same pool capacity
    on every compute and Nova does not re-claim ``DISK_GB`` on the
    destination during a shared-storage live migration, so an
    on-by-default ``DISK_GB`` gate would over-reject perfectly legal
    moves.  Local-disk clusters can opt in via
    ``[engine] enforce_placement_disk``.
    """

    vcpu: float = 0.0
    memory_mb: float = 0.0
    disk_gb: float = 0.0
    check_disk: bool = False

    def fits(self, claim: FlavorFootprint) -> bool:
        if self.vcpu < claim.vcpus:
            return False
        if self.memory_mb < claim.memory_mb:
            return False
        return not (self.check_disk and self.disk_gb < claim.disk_gb)

    def debit(self, claim: FlavorFootprint) -> None:
        self.vcpu -= claim.vcpus
        self.memory_mb -= claim.memory_mb
        if self.check_disk:
            self.disk_gb -= claim.disk_gb

    def credit(self, claim: FlavorFootprint) -> None:
        self.vcpu += claim.vcpus
        self.memory_mb += claim.memory_mb
        if self.check_disk:
            self.disk_gb += claim.disk_gb


@dataclass
class PlacementGate:
    """Per-cycle headroom ledger and pluggable destination check.

    The engine loop builds the ledger at cycle start (from
    ``PlacementClient.fetch_snapshots()``) and installs a flavor map
    keyed by instance UUID.  The constraint checker calls
    :meth:`is_destination_ok` for every candidate move; movers
    (evacuator, affinity enforcer, planner) call :meth:`commit_move`
    after each accepted move so the ledger stays consistent across
    speculative candidates within a cycle.

    ``account_disk`` mirrors ``[engine] enforce_placement_disk`` and
    controls whether ``DISK_GB`` participates in headroom decisions.
    Default is ``False`` so the gate behaves correctly on
    Ceph-backed clusters out of the box; set it to ``True`` only
    when ephemeral root disk is genuinely local.
    """

    enabled: bool = False
    account_disk: bool = False
    _headroom: dict[str, _Headroom] = field(default_factory=dict)
    _flavors: dict[str, FlavorFootprint] = field(default_factory=dict)
    _warned_missing_flavor: set[str] = field(default_factory=set)

    def set_snapshots(self, snapshots: dict[str, ProviderSnapshot]) -> None:
        """Install per-cycle headroom from placement provider snapshots.

        ``snapshots`` is the output of
        ``PlacementClient.fetch_snapshots()``: keyed by hostname, one
        :class:`~kronos.clients.placement.ProviderSnapshot` per
        provider.  Hosts not in this map fall through as "no entry"
        and the gate rejects them when enabled.
        """
        self._headroom = {
            host: _Headroom(
                vcpu=snap.headroom(RC_VCPU),
                memory_mb=snap.headroom(RC_MEMORY_MB),
                disk_gb=snap.headroom(RC_DISK_GB),
                check_disk=self.account_disk,
            )
            for host, snap in snapshots.items()
        }

    def set_flavors(self, flavors: dict[str, FlavorFootprint]) -> None:
        """Install per-VM flavor footprints for this cycle.

        Called by the engine loop after the profiler has collected
        instances for the aggregate under evaluation.  Successive
        aggregates extend the map - flavors don't change mid-cycle.
        """
        self._flavors.update(flavors)
        self._warned_missing_flavor.clear()

    def invalidate(self) -> None:
        """Drop the ledger and flavor map; called between cycles."""
        self._headroom = {}
        self._flavors = {}
        self._warned_missing_flavor = set()

    def is_destination_ok(self, instance_uuid: str, dest_host: str) -> bool:
        """Return True if ``dest_host`` has headroom for the VM's claim.

        Fails closed for unknown destinations, fails open for unknown
        VMs (and logs once per VM so the operator can investigate).
        Returns True unconditionally when the gate is disabled.
        """
        if not self.enabled:
            return True

        claim = self._flavors.get(instance_uuid)
        if claim is None:
            if instance_uuid not in self._warned_missing_flavor:
                LOG.warning(
                    "Placement gate has no flavor footprint for VM %s; "
                    "letting the move through and relying on Nova's claims "
                    "tracker at execute time.",
                    instance_uuid,
                )
                self._warned_missing_flavor.add(instance_uuid)
            return True

        headroom = self._headroom.get(dest_host)
        if headroom is None:
            LOG.debug(
                "Placement gate: destination %s has no inventory snapshot, "
                "rejecting VM %s (fails closed).",
                dest_host,
                instance_uuid,
            )
            return False

        if not headroom.fits(claim):
            disk_field = (
                f"disk_gb={claim.disk_gb}/{headroom.disk_gb:.1f}"
                if self.account_disk
                else "disk_gb=None"
            )
            LOG.debug(
                "Placement gate: destination %s lacks headroom for VM %s "
                "(vcpu=%d/%.1f, memory_mb=%d/%.1f, %s).",
                dest_host, instance_uuid,
                claim.vcpus, headroom.vcpu,
                claim.memory_mb, headroom.memory_mb,
                disk_field,
            )
            return False
        return True

    def commit_move(
        self,
        instance_uuid: str,
        from_host: str,
        to_host: str,
    ) -> None:
        """Debit destination and credit source headroom for an accepted move.

        Called by the mover (evacuator, affinity enforcer, planner)
        immediately after ``state.apply(...)``.  Keeps the ledger
        consistent so subsequent speculative candidates within the
        same cycle see truthful headroom.
        """
        if not self.enabled:
            return

        claim = self._flavors.get(instance_uuid)
        if claim is None:
            return

        dest = self._headroom.get(to_host)
        if dest is not None:
            dest.debit(claim)
        source = self._headroom.get(from_host)
        if source is not None:
            source.credit(claim)

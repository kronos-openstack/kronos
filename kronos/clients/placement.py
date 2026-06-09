"""OpenStack Placement API client for Kronos.

Reads resource provider inventories and usages so the engine can refuse
to plan a live migration whose flavor claim would exceed the
destination's placement headroom.  This is the data Nova consults at
``live-migrate`` time via the claims tracker; reading it ourselves at
plan time avoids ``400 No valid host was found`` errors that look fine
to the Prometheus-driven scorer but fail at execute time.

The client returns plain dataclasses (``ResourceInventory``,
``ResourceUsage``) keyed by hostname.  Higher layers in the engine
(``kronos.engine.placement.PlacementGate``) turn those into a per-host
headroom ledger and reason about it during planning.

The client is read-only.  It never modifies allocations - that is
Nova's job at live-migrate time.

Auth reuses the keystoneauth1 session already loaded for Nova via the
``[nova]`` config group, so no extra credentials are required as long
as the Kronos service user has ``placement:resource_providers:list``
and ``placement:resource_providers:inventories:list`` /
``placement:resource_providers:usages:list`` roles in Keystone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keystoneauth1 import loading as ks_loading
from openstack import connection as os_connection
from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import NOVA_GROUP
from kronos.common.exceptions import PlacementClientError

LOG = logging.getLogger(__name__)


# Resource class names we track.  Nova's claims tracker computes
# ``cpu_allocation_ratio`` against VCPU, ``ram_allocation_ratio``
# against MEMORY_MB, and ``disk_allocation_ratio`` against DISK_GB.
RC_VCPU = "VCPU"
RC_MEMORY_MB = "MEMORY_MB"
RC_DISK_GB = "DISK_GB"

TRACKED_RESOURCE_CLASSES = frozenset({RC_VCPU, RC_MEMORY_MB, RC_DISK_GB})


@dataclass
class ResourceInventory:
    """Inventory of one resource class on one resource provider.

    Effective capacity (the number Nova compares a claim against) is::

        (total - reserved) * allocation_ratio

    Headroom for a new claim is that capacity minus the current usage.
    """

    total: int = 0
    reserved: int = 0
    allocation_ratio: float = 1.0
    min_unit: int = 1
    max_unit: int = 0
    step_size: int = 1

    @property
    def effective_capacity(self) -> float:
        return float(self.total - self.reserved) * float(self.allocation_ratio)


@dataclass
class ProviderSnapshot:
    """Per-host placement snapshot: inventory + usage for tracked classes."""

    host: str
    inventories: dict[str, ResourceInventory] = field(default_factory=dict)
    usages: dict[str, int] = field(default_factory=dict)

    def headroom(self, resource_class: str) -> float:
        """Remaining capacity for one resource class, in claim units.

        Returns 0.0 when the host has no inventory for that class so
        callers fail closed for the (host, class) pair.
        """
        inv = self.inventories.get(resource_class)
        if inv is None:
            return 0.0
        return inv.effective_capacity - float(self.usages.get(resource_class, 0))


class PlacementClient:
    """Read-only wrapper around the OpenStack Placement API.

    Uses openstacksdk's ``placement`` proxy with the same keystoneauth1
    session Kronos already loads for Nova (``[nova]`` config group).
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        LOG.info(
            "Connecting to OpenStack Placement via keystoneauth1 [%s]",
            NOVA_GROUP,
        )
        try:
            auth = ks_loading.load_auth_from_conf_options(conf, NOVA_GROUP)
            session = ks_loading.load_session_from_conf_options(
                conf, NOVA_GROUP, auth=auth,
            )
            self._conn = os_connection.Connection(session=session)
        except Exception as exc:
            raise PlacementClientError(
                reason=f"Failed to connect: {exc}",
            ) from exc

    @property
    def _placement(self) -> Any:
        # openstacksdk exposes services as class-level ServiceDescription
        # descriptors that materialize a Proxy at runtime; static checkers
        # cannot follow that, so narrow to Any once here instead of
        # per-call ignores.
        return self._conn.placement

    def fetch_snapshots(self) -> dict[str, ProviderSnapshot]:
        """Return one ``ProviderSnapshot`` per compute resource provider.

        The map is keyed by the resource provider's ``name``, which is
        the hypervisor hostname (matching the ``host`` field on Nova's
        ``os-services`` entries).  Providers whose name does not match
        a compute host (sharing pools, parent providers, etc.) are
        still returned; callers select the ones that match their
        aggregate host list.

        :raises PlacementClientError: when the cluster-wide list fails.
            Per-provider inventory/usage failures are logged and produce
            an empty ``ProviderSnapshot`` for that host so a flaky
            provider doesn't pause planning cluster-wide.
        """
        try:
            providers = list(self._placement.resource_providers())
        except Exception as exc:
            raise PlacementClientError(
                reason=f"Failed to list resource providers: {exc}",
            ) from exc

        snapshots: dict[str, ProviderSnapshot] = {}
        for rp in providers:
            host = str(getattr(rp, "name", "") or "")
            if not host:
                continue
            snap = ProviderSnapshot(host=host)
            try:
                snap.inventories = self._fetch_inventories(rp.id)
            except Exception:
                LOG.warning(
                    "Failed to fetch placement inventories for '%s'; "
                    "treating host as zero-headroom.",
                    host,
                    exc_info=True,
                )
                snapshots[host] = snap
                continue
            try:
                snap.usages = self._fetch_usages(rp.id)
            except Exception:
                LOG.warning(
                    "Failed to fetch placement usages for '%s'; "
                    "treating host as zero-headroom.",
                    host,
                    exc_info=True,
                )
                snap.usages = {}
            snapshots[host] = snap
        return snapshots

    def _fetch_inventories(self, rp_id: str) -> dict[str, ResourceInventory]:
        result: dict[str, ResourceInventory] = {}
        for inv in self._placement.resource_provider_inventories(rp_id):
            rc = str(getattr(inv, "resource_class", "") or "")
            if rc not in TRACKED_RESOURCE_CLASSES:
                continue
            result[rc] = ResourceInventory(
                total=int(getattr(inv, "total", 0) or 0),
                reserved=int(getattr(inv, "reserved", 0) or 0),
                allocation_ratio=float(
                    getattr(inv, "allocation_ratio", 1.0) or 1.0,
                ),
                min_unit=int(getattr(inv, "min_unit", 1) or 1),
                max_unit=int(getattr(inv, "max_unit", 0) or 0),
                step_size=int(getattr(inv, "step_size", 1) or 1),
            )
        return result

    def _fetch_usages(self, rp_id: str) -> dict[str, int]:
        # openstacksdk's placement proxy does not expose a Resource
        # binding for the per-provider usages endpoint, so we hit the
        # raw REST route (GET /resource_providers/{uuid}/usages).  The
        # response shape is {"usages": {"VCPU": n, "MEMORY_MB": n, ...}}.
        resp = self._placement.get(f"/resource_providers/{rp_id}/usages")
        resp.raise_for_status()
        body = resp.json() or {}
        raw = body.get("usages") or {}
        return {
            rc: int(used or 0)
            for rc, used in raw.items()
            if rc in TRACKED_RESOURCE_CLASSES
        }

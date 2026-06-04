"""OpenStack Nova client for Kronos.

Authentication is configured via keystoneauth1 loading from the
``[nova]`` config group in ``kronos.conf``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import openstack
from keystoneauth1 import loading as ks_loading
from openstack import exceptions as os_exc
from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import NOVA_GROUP
from kronos.common.exceptions import (
    AggregateNotFound,
    NovaClientError,
    PlacementRejected,
)

LOG = logging.getLogger(__name__)


# Nova surfaces a placement / claims-tracker rejection as a 400-class
# response whose ``error_name`` (microversion >= 2.84) is ``NoValidHost``
# or whose body contains the ``noValidHost`` envelope.  We classify
# these as :class:`PlacementRejected` so the result listener can treat
# them as transient (free capacity may open up next cycle) instead of
# quarantining the VM the way it does for ``MigrationFailed`` /
# ``PreFlightError``.  Anything else is a genuine API error and stays
# :class:`NovaClientError`.
_PLACEMENT_REJECTION_NAMES = frozenset({"novalidhost", "noallocationonprovider"})
_PLACEMENT_REJECTION_KEYS = frozenset({"novalidhost"})


def _raise_for_live_migrate_http_error(
    exc: os_exc.HttpException,
    instance_uuid: str,
    dest_host: str,
) -> None:
    """Translate a Nova HTTP error into the right Kronos exception type."""
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    error_name = (getattr(exc, "error_name", "") or "").lower()
    body = getattr(exc, "details", None) or getattr(exc, "response_body", None)
    body_keys: set[str] = set()
    if isinstance(body, dict):
        body_keys = {str(k).lower() for k in body}

    is_placement = (
        error_name in _PLACEMENT_REJECTION_NAMES
        or bool(body_keys & _PLACEMENT_REJECTION_KEYS)
    )
    if is_placement and status in (400, 409):
        raise PlacementRejected(
            instance_uuid=instance_uuid,
            to_host=dest_host,
            reason=str(exc),
        ) from exc
    raise NovaClientError(
        reason=f"Failed to request live migration for {instance_uuid}: {exc}",
    ) from exc


@dataclass
class ComputeHost:
    """Simplified compute host representation."""

    name: str
    hypervisor_hostname: str
    state: str
    status: str
    vcpus: int
    vcpus_used: int
    memory_mb: int
    memory_mb_used: int
    running_vms: int


@dataclass
class Instance:
    """Simplified instance representation."""

    uuid: str
    name: str
    internal_name: str
    host: str
    flavor_vcpus: int
    flavor_ram_mb: int
    status: str
    flavor_disk_gb: int = 0
    server_groups: list[str] = field(default_factory=list)


@dataclass
class HostAggregate:
    """Nova host aggregate."""

    id: int
    name: str
    hosts: list[str]
    metadata: dict[str, str]


@dataclass
class ComputeService:
    """Nova compute service entry from the ``os-services`` API.

    ``state`` reflects the heartbeat freshness (``up`` or ``down``);
    ``status`` reflects the administrative gate (``enabled`` or
    ``disabled``).  A host is a valid live-migration destination only
    when ``state == 'up'`` and ``status == 'enabled'``.
    """

    host: str
    binary: str
    state: str
    status: str
    zone: str = ""
    disabled_reason: str = ""
    forced_down: bool = False

    @property
    def is_up(self) -> bool:
        return self.state == "up"

    @property
    def is_enabled(self) -> bool:
        return self.status == "enabled"

    @property
    def is_available_destination(self) -> bool:
        """Eligible to receive a live migration."""
        return self.is_up and self.is_enabled and not self.forced_down


class MigrationStatus(enum.StrEnum):
    """Nova live migration status values."""

    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    POST_MIGRATING = "post_migrating"
    COMPLETED = "completed"
    ERROR = "error"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            MigrationStatus.COMPLETED,
            MigrationStatus.ERROR,
            MigrationStatus.FAILED,
        }

    @property
    def is_success(self) -> bool:
        return self == MigrationStatus.COMPLETED


class NovaClient:
    """OpenStack Nova client.

    Uses openstacksdk with a keystoneauth1 session loaded from oslo.config.
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        LOG.info("Connecting to OpenStack via keystoneauth1 [%s]", NOVA_GROUP)
        try:
            auth = ks_loading.load_auth_from_conf_options(conf, NOVA_GROUP)
            session = ks_loading.load_session_from_conf_options(
                conf, NOVA_GROUP, auth=auth,
            )
            # Disable HTTP keep-alive on the Nova session.  The Nova API is
            # fronted by a VIP (HAProxy / eventlet wsgi) that closes idle
            # connections after a short timeout.  A pooled connection reused
            # after an idle gap - between migration-status polls in the
            # executor, or between cycles in the engine - lands on a
            # half-closed socket and raises "Remote end closed connection
            # without response".  keystoneauth retries and recovers, but
            # logs a WARNING every time.  Sending "Connection: close" makes
            # each request use a fresh connection so stale reuse never
            # happens; the extra TCP setup is negligible at Kronos's request
            # cadence.
            requests_session = getattr(session, "session", None)
            if requests_session is not None:
                requests_session.headers["Connection"] = "close"
            self._conn = openstack.connection.Connection(session=session)
        except Exception as exc:
            raise NovaClientError(reason=f"Failed to connect: {exc}") from exc

    def verify_connection(self) -> bool:
        """Verify Keystone authentication works.

        :returns: True if authentication is successful.
        :raises NovaClientError: If authentication fails.
        """
        try:
            self._conn.authorize()
            LOG.info("OpenStack authentication successful")
            return True
        except Exception as exc:
            raise NovaClientError(
                reason=f"Authentication failed: {exc}"
            ) from exc

    def list_aggregates(self) -> list[HostAggregate]:
        """List all host aggregates.

        :returns: List of HostAggregate instances.
        """
        try:
            aggregates = self._conn.compute.aggregates()
            return [
                HostAggregate(
                    id=agg.id,
                    name=agg.name,
                    hosts=list(agg.hosts or []),
                    metadata=dict(agg.metadata or {}),
                )
                for agg in aggregates
            ]
        except Exception as exc:
            raise NovaClientError(reason=f"Failed to list aggregates: {exc}") from exc

    def get_aggregate(self, aggregate_name: str) -> HostAggregate:
        """Get a specific host aggregate by name.

        :param aggregate_name: Name of the aggregate.
        :returns: HostAggregate instance.
        :raises AggregateNotFound: If the aggregate does not exist.
        """
        aggregates = self.list_aggregates()
        for agg in aggregates:
            if agg.name == aggregate_name:
                return agg
        raise AggregateNotFound(aggregate=aggregate_name)

    def get_hosts_in_aggregate(
        self, aggregate_name: str | None,
    ) -> list[str]:
        """Get hostnames for an aggregate or for the unassigned pool.

        :param aggregate_name: Name of the aggregate, or ``None`` to get
            compute hypervisors that are not members of any aggregate.
        :returns: List of hostnames.
        :raises AggregateNotFound: If a named aggregate does not exist.
        """
        if aggregate_name is None:
            all_hosts = {h.name for h in self.list_compute_hosts()}
            assigned: set[str] = set()
            for agg in self.list_aggregates():
                assigned.update(agg.hosts)
            return sorted(all_hosts - assigned)
        agg = self.get_aggregate(aggregate_name)
        return agg.hosts

    def list_compute_hosts(
        self, aggregate_name: str | None = None
    ) -> list[ComputeHost]:
        """List compute hosts, optionally filtered by aggregate.

        :param aggregate_name: If provided, only return hosts in this aggregate.
        :returns: List of ComputeHost instances.
        """
        try:
            hypervisors = list(self._conn.compute.hypervisors(details=True))
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to list hypervisors: {exc}"
            ) from exc

        # Only include VM hypervisors (QEMU/KVM), skip ironic bare-metal nodes
        hypervisor_types = {"QEMU", "qemu", "KVM", "kvm"}
        hosts = [
            ComputeHost(
                name=h.name,
                hypervisor_hostname=h.name,
                state=h.state,
                status=h.status,
                vcpus=h.vcpus or 0,
                vcpus_used=h.vcpus_used or 0,
                memory_mb=h.memory_size or 0,
                memory_mb_used=h.memory_used or 0,
                running_vms=h.running_vms or 0,
            )
            for h in hypervisors
            if (h.hypervisor_type or "") in hypervisor_types
        ]

        if aggregate_name:
            agg_hosts = set(self.get_hosts_in_aggregate(aggregate_name))
            hosts = [h for h in hosts if h.name in agg_hosts]

        return hosts

    def list_instances_on_host(self, host: str) -> list[Instance]:
        """List all instances on a given compute host.

        :param host: Compute host name.
        :returns: List of Instance instances.
        """
        try:
            servers = list(
                self._conn.compute.servers(
                    details=True,
                    all_projects=True,
                    compute_host=host,
                )
            )
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to list instances on host {host}: {exc}"
            ) from exc

        instances = []
        for s in servers:
            flavor = s.flavor or {}
            instances.append(
                Instance(
                    uuid=s.id,
                    name=s.name,
                    internal_name=getattr(s, "instance_name", s.id),
                    host=getattr(s, "hypervisor_hostname", host),
                    flavor_vcpus=flavor.get("vcpus", 0),
                    flavor_ram_mb=flavor.get("ram", 0),
                    flavor_disk_gb=flavor.get("disk", 0),
                    status=s.status,
                )
            )

        return instances

    def list_compute_services(self) -> list[ComputeService]:
        """List ``nova-compute`` service entries.

        Used by the engine to filter destinations to hosts whose
        compute service is up and administratively enabled, and by the
        executor to re-check service state at pre-flight time.

        :returns: List of ComputeService instances (one per nova-compute
            host).  Other binaries (``nova-conductor`` etc.) are filtered
            out.
        :raises NovaClientError: If the API call fails.
        """
        try:
            services = list(self._conn.compute.services())
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to list compute services: {exc}"
            ) from exc

        result: list[ComputeService] = []
        for s in services:
            if getattr(s, "binary", "") != "nova-compute":
                continue
            host = getattr(s, "host", "") or ""
            zone = str(getattr(s, "availability_zone", "") or "")
            if not zone:
                LOG.warning(
                    "nova-compute host '%s' has no availability_zone; "
                    "treating as unconfigured (will not match any "
                    "AZ-scoped engine).",
                    host,
                )
            result.append(
                ComputeService(
                    host=host,
                    binary=getattr(s, "binary", ""),
                    state=str(getattr(s, "state", "") or "").lower(),
                    status=str(getattr(s, "status", "") or "").lower(),
                    zone=zone,
                    disabled_reason=str(getattr(s, "disabled_reason", "") or ""),
                    forced_down=bool(getattr(s, "forced_down", False)),
                ),
            )
        return result

    def list_server_groups(self) -> list[dict[str, object]]:
        """List server groups across all projects.

        Nova exposes both the legacy ``policy`` (singular string) and the
        current ``policies`` (list) attribute depending on the API
        microversion.  Older deployments populate only the singular field;
        newer ones populate the list.  We merge both into a ``policies``
        list so the constraint checker has a single shape to reason about.

        Nova API microversion 2.64 also added a ``rules`` dict.  The only
        rule defined upstream is ``max_server_per_host``, valid for the
        ``anti-affinity`` policy: it caps how many group members may share
        a single host (default 1 = strict anti-affinity).  We pass the
        dict through unchanged; clouds older than 2.64 report no rules and
        the constraint checker falls back to the strict default.

        :returns: List of server group dicts with keys id, name, policies,
            members, rules.
        """
        try:
            groups = list(self._conn.compute.server_groups(all_projects=True))
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to list server groups: {exc}"
            ) from exc

        result: list[dict[str, object]] = []
        for g in groups:
            policies: list[str] = []
            raw_policies = getattr(g, "policies", None)
            if raw_policies:
                policies.extend(str(p) for p in raw_policies)
            raw_policy = getattr(g, "policy", None)
            if raw_policy and str(raw_policy) not in policies:
                policies.append(str(raw_policy))

            raw_rules = getattr(g, "rules", None)
            rules = dict(raw_rules) if isinstance(raw_rules, dict) else {}

            result.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "policies": policies,
                    "members": list(g.member_ids or []),
                    "rules": rules,
                },
            )
        return result

    # --- Write operations ---

    def get_instance_status(
        self, instance_uuid: str,
    ) -> tuple[str, str | None]:
        """Get instance status and task_state for pre-flight checks.

        :param instance_uuid: The instance UUID.
        :returns: Tuple of (status, task_state). task_state is None when idle.
        :raises NovaClientError: If the API call fails.
        """
        try:
            server = self._conn.compute.get_server(instance_uuid)
            return server.status, getattr(server, "task_state", None)
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to get instance {instance_uuid}: {exc}"
            ) from exc

    def get_instance_host(self, instance_uuid: str) -> str:
        """Get the current compute host for an instance.

        :param instance_uuid: The instance UUID.
        :returns: Hostname the instance is running on.
        :raises NovaClientError: If the API call fails.
        """
        try:
            server = self._conn.compute.get_server(instance_uuid)
            return getattr(server, "hypervisor_hostname", "") or ""
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to get instance {instance_uuid}: {exc}"
            ) from exc

    def live_migrate(
        self,
        instance_uuid: str,
        dest_host: str,
        block_migration: str = "auto",
    ) -> None:
        """Request a live migration via the Nova API.

        :param instance_uuid: The instance to migrate.
        :param dest_host: Destination compute host.
        :param block_migration: "auto", "true", or "false".
        :raises NovaClientError: If the API call fails.
        """
        try:
            self._conn.compute.live_migrate_server(
                instance_uuid,
                host=dest_host,
                block_migration=block_migration,
            )
            LOG.info(
                "Live migration requested: %s -> %s",
                instance_uuid,
                dest_host,
            )
        except os_exc.HttpException as exc:
            _raise_for_live_migrate_http_error(exc, instance_uuid, dest_host)
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to request live migration for {instance_uuid}: {exc}"
            ) from exc

    def get_migration_status(
        self, instance_uuid: str,
    ) -> MigrationStatus | None:
        """Get the status of any in-progress migration for an instance.

        Uses the server migrations API (``GET /servers/{id}/migrations``)
        which only returns **active** migrations - not historical ones.
        Once a migration completes it disappears from this endpoint.

        :param instance_uuid: The instance UUID.
        :returns: MigrationStatus if an active migration exists, None otherwise.
        :raises NovaClientError: If the API call fails.
        """
        try:
            migrations = list(
                self._conn.compute.server_migrations(instance_uuid),
            )
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to get migrations for {instance_uuid}: {exc}"
            ) from exc

        if not migrations:
            return None

        latest = migrations[-1]
        raw_status = str(getattr(latest, "status", "error")).lower()
        try:
            return MigrationStatus(raw_status)
        except ValueError:
            LOG.warning(
                "Unknown migration status '%s' for %s, treating as error.",
                raw_status,
                instance_uuid,
            )
            return MigrationStatus.ERROR

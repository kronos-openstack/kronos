"""OpenStack Nova client for Kronos.

Authentication is configured via keystoneauth1 loading from the
``[nova]`` config group in ``kronos.conf``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import openstack
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from oslo_log import log as logging

from kronos.common.config import NOVA_GROUP
from kronos.common.exceptions import AggregateNotFound, NovaClientError

LOG = logging.getLogger(__name__)


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
    server_groups: list[str] = field(default_factory=list)


@dataclass
class HostAggregate:
    """Nova host aggregate."""

    id: int
    name: str
    hosts: list[str]
    metadata: dict[str, str]


class NovaClient:
    """OpenStack Nova client.

    Uses openstacksdk with a keystoneauth1 session loaded from oslo.config.
    Read-only in M1 — live-migrate calls will be added in M3.
    """

    def __init__(self, conf: cfg.ConfigOpts) -> None:
        LOG.info("Connecting to OpenStack via keystoneauth1 [%s]", NOVA_GROUP)
        try:
            auth = ks_loading.load_auth_from_conf_options(conf, NOVA_GROUP)
            session = ks_loading.load_session_from_conf_options(
                conf, NOVA_GROUP, auth=auth,
            )
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

    def get_aggregate_hosts(self, aggregate_name: str) -> list[str]:
        """Get hostnames in a specific aggregate.

        :param aggregate_name: Name of the aggregate.
        :returns: List of hostnames.
        :raises AggregateNotFound: If the aggregate does not exist.
        """
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

        hosts = [
            ComputeHost(
                name=h.name,
                hypervisor_hostname=h.hypervisor_hostname,
                state=h.state,
                status=h.status,
                vcpus=h.vcpus or 0,
                vcpus_used=h.vcpus_used or 0,
                memory_mb=h.memory_size or 0,
                memory_mb_used=h.memory_used or 0,
                running_vms=h.running_vms or 0,
            )
            for h in hypervisors
        ]

        if aggregate_name:
            agg_hosts = set(self.get_aggregate_hosts(aggregate_name))
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
                    host=host,
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
                    status=s.status,
                )
            )

        return instances

    def list_server_groups(self) -> list[dict[str, object]]:
        """List server groups (for anti-affinity constraints in M2+).

        :returns: List of server group dicts.
        """
        try:
            groups = list(self._conn.compute.server_groups())
            return [
                {
                    "id": g.id,
                    "name": g.name,
                    "policies": list(g.policies or []),
                    "members": list(g.member_ids or []),
                }
                for g in groups
            ]
        except Exception as exc:
            raise NovaClientError(
                reason=f"Failed to list server groups: {exc}"
            ) from exc

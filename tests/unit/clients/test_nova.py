"""Tests for the Nova (OpenStack) client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kronos.clients.nova import (
    ComputeHost,
    ComputeService,
    HostAggregate,
    Instance,
    NovaClient,
)
from kronos.common.exceptions import AggregateNotFound, NovaClientError


def _make_mock_aggregate(name="test-agg", agg_id=1, hosts=None, metadata=None):
    agg = MagicMock()
    agg.id = agg_id
    agg.name = name
    agg.hosts = hosts or ["compute-01", "compute-02"]
    agg.metadata = metadata or {}
    return agg


def _make_mock_hypervisor(
    name="compute-01",
    hostname="compute-01.local",
    state="up",
    status="enabled",
    vcpus=64,
    vcpus_used=32,
    memory_size=131072,
    memory_used=65536,
    running_vms=10,
    hypervisor_type="QEMU",
):
    hv = MagicMock()
    hv.name = name
    hv.hypervisor_hostname = hostname
    hv.hypervisor_type = hypervisor_type
    hv.state = state
    hv.status = status
    hv.vcpus = vcpus
    hv.vcpus_used = vcpus_used
    hv.memory_size = memory_size
    hv.memory_used = memory_used
    hv.running_vms = running_vms
    return hv


def _make_mock_server(
    server_id="uuid-1",
    name="test-vm",
    instance_name="instance-00001",
    hypervisor_hostname="compute-01",
    vcpus=4,
    ram=8192,
    status="ACTIVE",
):
    server = MagicMock()
    server.id = server_id
    server.name = name
    server.instance_name = instance_name
    server.hypervisor_hostname = hypervisor_hostname
    server.flavor = {"vcpus": vcpus, "ram": ram}
    server.status = status
    return server


@pytest.fixture()
def mock_nova_client():
    """Create a NovaClient with mocked openstack connection."""
    with patch("kronos.clients.nova.ks_loading") as mock_ks:
        mock_auth = MagicMock()
        mock_session = MagicMock()
        mock_ks.load_auth_from_conf_options.return_value = mock_auth
        mock_ks.load_session_from_conf_options.return_value = mock_session

        with patch("kronos.clients.nova.openstack.connection.Connection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn_cls.return_value = mock_conn

            conf = MagicMock()
            client = NovaClient(conf)
            client._mock_conn = mock_conn
            yield client


class TestNovaClientInit:
    def test_connection_failure(self):
        with patch("kronos.clients.nova.ks_loading") as mock_ks:
            mock_ks.load_auth_from_conf_options.side_effect = Exception("auth failed")
            with pytest.raises(NovaClientError, match="Failed to connect"):
                NovaClient(MagicMock())


class TestVerifyConnection:
    def test_success(self, mock_nova_client):
        mock_nova_client._mock_conn.authorize.return_value = None
        assert mock_nova_client.verify_connection() is True

    def test_failure(self, mock_nova_client):
        mock_nova_client._mock_conn.authorize.side_effect = Exception("unauthorized")
        with pytest.raises(NovaClientError, match="Authentication failed"):
            mock_nova_client.verify_connection()


class TestListAggregates:
    def test_returns_aggregates(self, mock_nova_client):
        mock_agg = _make_mock_aggregate(name="gpu-agg", hosts=["h1", "h2"])
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        result = mock_nova_client.list_aggregates()

        assert len(result) == 1
        assert isinstance(result[0], HostAggregate)
        assert result[0].name == "gpu-agg"
        assert result[0].hosts == ["h1", "h2"]

    def test_empty_list(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.aggregates.return_value = []
        assert mock_nova_client.list_aggregates() == []

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.aggregates.side_effect = Exception("API down")
        with pytest.raises(NovaClientError, match="Failed to list aggregates"):
            mock_nova_client.list_aggregates()


class TestGetAggregate:
    def test_found(self, mock_nova_client):
        mock_agg = _make_mock_aggregate(name="target")
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        result = mock_nova_client.get_aggregate("target")
        assert result.name == "target"

    def test_not_found(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.aggregates.return_value = []
        with pytest.raises(AggregateNotFound, match="not found"):
            mock_nova_client.get_aggregate("nonexistent")


class TestGetHostsInAggregate:
    def test_returns_host_list(self, mock_nova_client):
        mock_agg = _make_mock_aggregate(hosts=["h1", "h2", "h3"])
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        hosts = mock_nova_client.get_hosts_in_aggregate("test-agg")
        assert hosts == ["h1", "h2", "h3"]

    def test_none_returns_unassigned(self, mock_nova_client):
        # One aggregate contains h1 only; h2 is unassigned
        mock_agg = _make_mock_aggregate(hosts=["h1"])
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        hv1 = _make_mock_hypervisor(name="h1")
        hv2 = _make_mock_hypervisor(name="h2")
        mock_nova_client._mock_conn.compute.hypervisors.return_value = [hv1, hv2]

        hosts = mock_nova_client.get_hosts_in_aggregate(None)
        assert hosts == ["h2"]


class TestListComputeHosts:
    def test_all_hosts(self, mock_nova_client):
        hv1 = _make_mock_hypervisor(name="h1")
        hv2 = _make_mock_hypervisor(name="h2")
        mock_nova_client._mock_conn.compute.hypervisors.return_value = [hv1, hv2]

        result = mock_nova_client.list_compute_hosts()
        assert len(result) == 2
        assert all(isinstance(h, ComputeHost) for h in result)

    def test_filter_by_aggregate(self, mock_nova_client):
        hv1 = _make_mock_hypervisor(name="h1")
        hv2 = _make_mock_hypervisor(name="h2")
        hv3 = _make_mock_hypervisor(name="h3")
        mock_nova_client._mock_conn.compute.hypervisors.return_value = [hv1, hv2, hv3]

        mock_agg = _make_mock_aggregate(hosts=["h1", "h3"])
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        result = mock_nova_client.list_compute_hosts(aggregate_name="test-agg")
        names = [h.name for h in result]
        assert "h1" in names
        assert "h3" in names
        assert "h2" not in names

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.hypervisors.side_effect = Exception("fail")
        with pytest.raises(NovaClientError, match="Failed to list hypervisors"):
            mock_nova_client.list_compute_hosts()


class TestListInstancesOnHost:
    def test_returns_instances(self, mock_nova_client):
        s1 = _make_mock_server(server_id="u1", name="vm-1")
        s2 = _make_mock_server(server_id="u2", name="vm-2")
        mock_nova_client._mock_conn.compute.servers.return_value = [s1, s2]

        result = mock_nova_client.list_instances_on_host("compute-01")
        assert len(result) == 2
        assert all(isinstance(i, Instance) for i in result)
        assert result[0].uuid == "u1"
        assert result[0].name == "vm-1"

    def test_empty_host(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.servers.return_value = []
        result = mock_nova_client.list_instances_on_host("empty-host")
        assert result == []

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.servers.side_effect = Exception("fail")
        with pytest.raises(NovaClientError, match="Failed to list instances"):
            mock_nova_client.list_instances_on_host("compute-01")


class TestListServerGroups:
    def test_returns_groups_with_policies_list(self, mock_nova_client):
        """Modern microversion uses the 'policies' list attribute."""
        group = MagicMock()
        group.id = "sg-1"
        group.name = "anti-affinity-group"
        group.policies = ["anti-affinity"]
        group.policy = None
        group.member_ids = ["uuid-1", "uuid-2"]
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert len(result) == 1
        assert result[0]["name"] == "anti-affinity-group"
        assert result[0]["policies"] == ["anti-affinity"]
        assert result[0]["members"] == ["uuid-1", "uuid-2"]

    def test_returns_groups_with_legacy_policy_attr(self, mock_nova_client):
        """Older Nova deployments only populate the singular 'policy' attribute."""
        group = MagicMock()
        group.id = "sg-2"
        group.name = "soft-group"
        group.policies = None
        group.policy = "soft-anti-affinity"
        group.member_ids = ["uuid-3"]
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert result[0]["policies"] == ["soft-anti-affinity"]

    def test_merges_legacy_and_modern_attrs(self, mock_nova_client):
        """Both attributes populated → merge without duplicates."""
        group = MagicMock()
        group.id = "sg-3"
        group.name = "both"
        group.policies = ["anti-affinity"]
        group.policy = "anti-affinity"
        group.member_ids = []
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert result[0]["policies"] == ["anti-affinity"]

    def test_returns_rules_dict(self, mock_nova_client):
        """The 2.64 'rules' dict is passed through unchanged."""
        group = MagicMock()
        group.id = "sg-4"
        group.name = "capped"
        group.policies = ["anti-affinity"]
        group.policy = None
        group.member_ids = ["uuid-1"]
        group.rules = {"max_server_per_host": 3}
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert result[0]["rules"] == {"max_server_per_host": 3}

    def test_missing_rules_defaults_to_empty_dict(self, mock_nova_client):
        """Pre-2.64 clouds report no rules; we normalise to an empty dict."""
        group = MagicMock()
        group.id = "sg-5"
        group.name = "no-rules"
        group.policies = ["anti-affinity"]
        group.policy = None
        group.member_ids = ["uuid-1"]
        group.rules = None
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert result[0]["rules"] == {}

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.server_groups.side_effect = Exception("fail")
        with pytest.raises(NovaClientError, match="Failed to list server groups"):
            mock_nova_client.list_server_groups()


def _make_mock_service(
    host="compute-01",
    binary="nova-compute",
    state="up",
    status="enabled",
    availability_zone="nova",
    disabled_reason="",
    forced_down=False,
):
    svc = MagicMock()
    svc.host = host
    svc.binary = binary
    svc.state = state
    svc.status = status
    svc.availability_zone = availability_zone
    svc.disabled_reason = disabled_reason
    svc.forced_down = forced_down
    return svc


class TestListComputeServices:
    def test_filters_to_nova_compute(self, mock_nova_client):
        """Only nova-compute entries are returned; conductor/scheduler dropped."""
        services = [
            _make_mock_service(host="h1", binary="nova-compute"),
            _make_mock_service(host="h2", binary="nova-conductor"),
            _make_mock_service(host="h3", binary="nova-compute"),
            _make_mock_service(host="h4", binary="nova-scheduler"),
        ]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()

        assert len(result) == 2
        assert {s.host for s in result} == {"h1", "h3"}
        assert all(isinstance(s, ComputeService) for s in result)

    def test_normalises_state_and_status_to_lowercase(self, mock_nova_client):
        """Some Nova versions return upper-case enum values; lowercase them."""
        services = [_make_mock_service(state="UP", status="ENABLED")]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()

        assert result[0].state == "up"
        assert result[0].status == "enabled"
        assert result[0].is_available_destination is True

    def test_disabled_host_not_available_destination(self, mock_nova_client):
        services = [_make_mock_service(state="up", status="disabled")]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()
        assert result[0].is_up is True
        assert result[0].is_enabled is False
        assert result[0].is_available_destination is False

    def test_down_host_not_available_destination(self, mock_nova_client):
        services = [_make_mock_service(state="down", status="enabled")]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()
        assert result[0].is_up is False
        assert result[0].is_available_destination is False

    def test_forced_down_blocks_destination_even_when_state_up(
        self, mock_nova_client,
    ):
        services = [
            _make_mock_service(state="up", status="enabled", forced_down=True),
        ]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()
        assert result[0].forced_down is True
        assert result[0].is_available_destination is False

    def test_zone_parsed_from_availability_zone(self, mock_nova_client):
        services = [
            _make_mock_service(host="h1", availability_zone="nova"),
            _make_mock_service(host="h2", availability_zone="dmz"),
        ]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()
        zones = {s.host: s.zone for s in result}
        assert zones == {"h1": "nova", "h2": "dmz"}

    def test_missing_zone_warns_and_stays_empty(
        self, mock_nova_client, caplog,
    ):
        services = [_make_mock_service(host="h1", availability_zone="")]
        mock_nova_client._mock_conn.compute.services.return_value = services

        with caplog.at_level("WARNING"):
            result = mock_nova_client.list_compute_services()

        assert result[0].zone == ""
        assert any(
            "h1" in r.message and "availability_zone" in r.message
            for r in caplog.records
        )

    def test_disabled_reason_carried_through(self, mock_nova_client):
        services = [
            _make_mock_service(
                state="up", status="disabled", disabled_reason="maintenance",
            ),
        ]
        mock_nova_client._mock_conn.compute.services.return_value = services

        result = mock_nova_client.list_compute_services()
        assert result[0].disabled_reason == "maintenance"

    def test_empty_list(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.services.return_value = []
        assert mock_nova_client.list_compute_services() == []

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.services.side_effect = Exception("fail")
        with pytest.raises(NovaClientError, match="Failed to list compute services"):
            mock_nova_client.list_compute_services()

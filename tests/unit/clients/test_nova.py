"""Tests for the Nova (OpenStack) client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kronos.clients.nova import (
    ComputeHost,
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
):
    hv = MagicMock()
    hv.name = name
    hv.hypervisor_hostname = hostname
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


class TestGetAggregateHosts:
    def test_returns_host_list(self, mock_nova_client):
        mock_agg = _make_mock_aggregate(hosts=["h1", "h2", "h3"])
        mock_nova_client._mock_conn.compute.aggregates.return_value = [mock_agg]

        hosts = mock_nova_client.get_aggregate_hosts("test-agg")
        assert hosts == ["h1", "h2", "h3"]


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
    def test_returns_groups(self, mock_nova_client):
        group = MagicMock()
        group.id = "sg-1"
        group.name = "anti-affinity-group"
        group.policies = ["anti-affinity"]
        group.member_ids = ["uuid-1", "uuid-2"]
        mock_nova_client._mock_conn.compute.server_groups.return_value = [group]

        result = mock_nova_client.list_server_groups()
        assert len(result) == 1
        assert result[0]["name"] == "anti-affinity-group"
        assert result[0]["policies"] == ["anti-affinity"]
        assert result[0]["members"] == ["uuid-1", "uuid-2"]

    def test_api_error(self, mock_nova_client):
        mock_nova_client._mock_conn.compute.server_groups.side_effect = Exception("fail")
        with pytest.raises(NovaClientError, match="Failed to list server groups"):
            mock_nova_client.list_server_groups()

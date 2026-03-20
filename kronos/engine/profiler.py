"""VM profile collection: maps Prometheus per-VM metrics to Nova instances."""

from __future__ import annotations

from collections import Counter

from oslo_log import log as logging

from kronos.clients.nova import Instance, NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.engine.types import VmProfile
from kronos.policies.models import (
    PolicyConfig,
    VmProfileFallback,
    VmProfileLabelType,
)

LOG = logging.getLogger(__name__)


class VmProfiler:
    """Collects per-VM resource profiles for planner simulation.

    Queries ``vm_profile_query`` from Prometheus, maps the results to
    Nova instances using ``vm_profile_label_type``, and applies fallback
    strategies for VMs without Prometheus data.
    """

    def __init__(
        self,
        prometheus: PrometheusClient,
        nova: NovaClient,
    ) -> None:
        self._prometheus = prometheus
        self._nova = nova

    def collect(
        self,
        policy: PolicyConfig,
        aggregate_hosts: list[str],
        host_scores: dict[str, float],
    ) -> dict[str, VmProfile]:
        """Collect VM profiles for all instances in the aggregate.

        :param policy: Policy with vm_profile_query and mapping config.
        :param aggregate_hosts: Hostnames in the policy's aggregate.
        :param host_scores: Raw per-host scores from imbalance_query.
        :returns: Mapping of instance UUID to VmProfile.
        """
        instances = self._collect_instances(aggregate_hosts)
        if not instances:
            LOG.warning(
                "Policy '%s': no instances found on aggregate hosts.",
                policy.name,
            )
            return {}

        # Count VMs per host for HOST_AVERAGE fallback
        vms_per_host: Counter[str] = Counter(i.host for i in instances)

        # Query Prometheus for per-VM metrics
        prom_profiles: dict[str, float] = {}
        if policy.vm_profile_query:
            prom_profiles = self._query_vm_profiles(
                policy.vm_profile_query,
                policy.vm_profile_label,
            )

        profiles: dict[str, VmProfile] = {}
        for instance in instances:
            prom_key = _instance_prom_key(instance, policy.vm_profile_label_type)
            weight: float | None = prom_profiles.get(prom_key)
            source = "prometheus"

            if weight is None:
                weight = _apply_fallback(
                    instance=instance,
                    fallback=policy.vm_profile_fallback,
                    host_scores=host_scores,
                    vms_per_host=vms_per_host,
                )
                source = f"fallback:{policy.vm_profile_fallback.value}"

            if weight is None:
                continue

            profiles[instance.uuid] = VmProfile(
                instance_uuid=instance.uuid,
                instance_name=instance.name,
                host=instance.host,
                weight=weight,
                source=source,
            )

        LOG.info(
            "Policy '%s': collected %d VM profiles (%d instances total).",
            policy.name,
            len(profiles),
            len(instances),
        )
        return profiles

    def _collect_instances(
        self,
        aggregate_hosts: list[str],
    ) -> list[Instance]:
        """Fetch all instances across aggregate hosts."""
        instances: list[Instance] = []
        for host in aggregate_hosts:
            instances.extend(self._nova.list_instances_on_host(host))
        return instances

    def _query_vm_profiles(
        self,
        query: str,
        label_key: str,
    ) -> dict[str, float]:
        """Query Prometheus for per-VM metrics.

        Returns a mapping from label value to metric value.
        No expected_labels check — partial VM data is normal.
        """
        result = self._prometheus.instant_query(
            query=query,
            label_key=label_key,
            expected_labels=None,
        )
        return result.series


def _instance_prom_key(
    instance: Instance,
    label_type: VmProfileLabelType,
) -> str:
    """Map a Nova instance to the Prometheus label value."""
    if label_type == VmProfileLabelType.NOVA_INTERNAL_NAME:
        return instance.internal_name
    if label_type == VmProfileLabelType.NOVA_INSTANCE_UUID:
        return instance.uuid
    if label_type == VmProfileLabelType.NOVA_DISPLAY_NAME:
        return instance.name
    return instance.internal_name


def _apply_fallback(
    instance: Instance,
    fallback: VmProfileFallback,
    host_scores: dict[str, float],
    vms_per_host: Counter[str],
) -> float | None:
    """Apply fallback strategy for VMs without Prometheus data.

    :param instance: The Nova instance.
    :param fallback: Which fallback strategy to use.
    :param host_scores: Raw per-host scores from imbalance_query.
    :param vms_per_host: Count of VMs per host.
    :returns: Estimated weight, or None if the VM should be skipped.
    """
    if fallback == VmProfileFallback.SKIP:
        return None

    if fallback == VmProfileFallback.FLAVOR_VCPU_RATIO:
        # Estimate weight proportional to VM's vCPU share of the host.
        # Use the host's score * (VM vCPUs / total vCPUs on host) as proxy.
        host_score = host_scores.get(instance.host, 0.0)
        vm_count = vms_per_host.get(instance.host, 1)
        return host_score * instance.flavor_vcpus / max(instance.flavor_vcpus * vm_count, 1)

    if fallback == VmProfileFallback.HOST_AVERAGE:
        # Assume each VM contributes equally to the host's score.
        host_score = host_scores.get(instance.host, 0.0)
        vm_count = vms_per_host.get(instance.host, 1)
        return host_score / max(vm_count, 1)

    return None

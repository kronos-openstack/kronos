"""VM profile collection: per-aggregate, across all policies.

One profiler run builds a ``dict[uuid, VmProfile]`` for the aggregate with
per-policy ``weights`` and ``sources`` dicts.  This is the data the planner
uses to simulate combined-scoring moves.
"""

from __future__ import annotations

from collections import Counter

from oslo_log import log as logging

from kronos.clients.nova import Instance, NovaClient
from kronos.clients.prometheus import PrometheusClient
from kronos.engine.placement import FlavorFootprint
from kronos.engine.types import VmProfile
from kronos.policies.models import (
    PolicyConfig,
    VmProfileFallback,
    VmProfileLabelType,
)

LOG = logging.getLogger(__name__)


class VmProfiler:
    """Collects per-VM resource profiles across all policies for an aggregate.

    Each policy contributes its own view of each VM (memory RSS ratio, CPU
    rate ratio, etc.).  VMs without Prometheus data for a given policy fall
    back per that policy's ``vm_profile_fallback`` setting; a VM may end up
    with data from some policies and fallback from others.
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
        policies: list[PolicyConfig],
        hosts: list[str],
        host_scores_by_policy: dict[str, dict[str, float]],
        flavor_sink: dict[str, FlavorFootprint] | None = None,
    ) -> dict[str, VmProfile]:
        """Collect VM profiles for all instances on the aggregate's hosts.

        :param policies: Enabled policies for this aggregate.
        :param hosts: Hostnames in the aggregate.
        :param host_scores_by_policy: ``{policy_name: {host: raw_score}}``
            (used by fallback strategies).
        :param flavor_sink: Optional mapping that, when provided, is
            populated with one :class:`~kronos.engine.placement.FlavorFootprint`
            per *every* listed instance (vcpus / ram_mb / disk_gb from the
            instance's Nova flavor).  Filled regardless of whether the VM
            survives the per-policy fallback drop, so the placement gate
            can still rate-limit candidates the planner didn't end up
            promoting.  Callers that don't care can omit it.
        :returns: Mapping of instance UUID -> VmProfile (with per-policy weights).
        """
        instances = self._collect_instances(hosts)
        if not instances:
            LOG.warning("No instances found on hosts %s", hosts)
            return {}

        vms_per_host: Counter[str] = Counter(i.host for i in instances)

        # Query Prometheus once per policy that has a vm_profile_query
        prom_results: dict[str, dict[str, float]] = {}
        for policy in policies:
            if policy.vm_profile_query:
                prom_results[policy.name] = self._query_vm_profiles(
                    policy.vm_profile_query,
                    policy.vm_profile_label,
                )
            else:
                prom_results[policy.name] = {}

        profiles: dict[str, VmProfile] = {}
        for instance in instances:
            if flavor_sink is not None:
                flavor_sink[instance.uuid] = FlavorFootprint(
                    vcpus=instance.flavor_vcpus,
                    memory_mb=instance.flavor_ram_mb,
                    disk_gb=instance.flavor_disk_gb,
                )
            weights: dict[str, float] = {}
            sources: dict[str, str] = {}
            drop = False

            for policy in policies:
                prom_key = _instance_prom_key(instance, policy.vm_profile_label_type)
                raw = prom_results[policy.name].get(prom_key)
                source = "prometheus"

                if raw is None:
                    raw = _apply_fallback(
                        instance=instance,
                        fallback=policy.vm_profile_fallback,
                        host_scores=host_scores_by_policy.get(policy.name, {}),
                        vms_per_host=vms_per_host,
                    )
                    source = f"fallback:{policy.vm_profile_fallback.value}"

                if raw is None:
                    # Policy says SKIP and no Prometheus data: drop this VM
                    # from the combined profile entirely.
                    drop = True
                    break

                weights[policy.name] = raw
                sources[policy.name] = source

            if drop:
                continue

            profiles[instance.uuid] = VmProfile(
                instance_uuid=instance.uuid,
                instance_name=instance.name,
                host=instance.host,
                weights=weights,
                sources=sources,
            )

        LOG.info(
            "Collected %d VM profiles (%d instances total, %d policies).",
            len(profiles),
            len(instances),
            len(policies),
        )
        return profiles

    def _collect_instances(self, hosts: list[str]) -> list[Instance]:
        instances: list[Instance] = []
        skipped: list[tuple[str, str]] = []
        for host in hosts:
            for inst in self._nova.list_instances_on_host(host):
                if inst.status != "ACTIVE":
                    skipped.append((inst.uuid, inst.status))
                    continue
                instances.append(inst)
        if skipped:
            LOG.debug(
                "Skipped %d non-ACTIVE VM(s) (only ACTIVE instances are "
                "live-migratable): %s",
                len(skipped),
                skipped,
            )
        return instances

    def _query_vm_profiles(
        self,
        query: str,
        label_key: str,
    ) -> dict[str, float]:
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
    """Apply fallback strategy for VMs without Prometheus data."""
    if fallback == VmProfileFallback.SKIP:
        return None

    if fallback == VmProfileFallback.FLAVOR_VCPU_RATIO:
        host_score = host_scores.get(instance.host, 0.0)
        vm_count = vms_per_host.get(instance.host, 1)
        return host_score * instance.flavor_vcpus / max(instance.flavor_vcpus * vm_count, 1)

    if fallback == VmProfileFallback.HOST_AVERAGE:
        host_score = host_scores.get(instance.host, 0.0)
        vm_count = vms_per_host.get(instance.host, 1)
        return host_score / max(vm_count, 1)

    return None

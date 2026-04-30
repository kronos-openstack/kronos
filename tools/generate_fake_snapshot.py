"""Generate a synthetic Kronos snapshot for performance benchmarking.

The output directory has the exact layout produced by ``kronos-record``
(see :mod:`kronos.cmd.record`), so the existing ``kronos-replay`` path
consumes it unchanged - benchmark hits the real scorer, profiler,
constraints, planner, and enforcer, no mocks.

All distributions are independent Gaussians clipped to sensible
ranges; pass ``--seed`` for reproducible runs.

Usage:
    python tools/generate_fake_snapshot.py \\
        --hosts 50 --vms 5000 --groups 100 \\
        /tmp/snapshot-fake

Then replay it:
    kronos-replay --config-file /tmp/kronos.conf /tmp/snapshot-fake
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

# Kronos uses "_unassigned_" as the synthetic aggregate name for the
# unassigned-hosts pool.  Kept in sync with kronos.common.messaging.
UNASSIGNED_TOPIC_MARKER = "_unassigned_"


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _gauss_clipped(
    rng: random.Random, mean: float, stdev: float, low: float, high: float,
) -> float:
    return _clip(rng.gauss(mean, stdev), low, high)


def _gauss_int(
    rng: random.Random, mean: float, stdev: float, low: int, high: int,
) -> int:
    return round(_gauss_clipped(rng, mean, stdev, low, high))


def generate(
    out_dir: Path,
    hosts: int,
    vms: int,
    groups: int,
    host_score_mean: float,
    host_score_stdev: float,
    vm_weight_mean: float,
    vm_weight_stdev: float,
    group_size_mean: float,
    group_size_stdev: float,
    seed: int,
) -> None:
    """Write a synthetic snapshot to ``out_dir``.

    The snapshot ships two synthetic policies - ``cpu-spread`` and
    ``memory-spread`` - matching the shape of the sample
    ``policies.yaml`` in ``internal-documentation/``.  Benchmark
    against that file.
    """
    if out_dir.exists():
        raise SystemExit(f"Output directory already exists: {out_dir}")
    out_dir.mkdir(parents=True)

    rng = random.Random(seed)

    host_names = [f"fake-host-{i:04d}" for i in range(hosts)]

    # --- Meta ---------------------------------------------------------
    (out_dir / "meta.json").write_text(
        json.dumps(
            {
                "recorded_at": datetime.now(tz=UTC).isoformat(),
                "synthetic": True,
                "generator": "tools/generate_fake_snapshot.py",
                "seed": seed,
                "params": {
                    "hosts": hosts,
                    "vms": vms,
                    "groups": groups,
                    "host_score_mean": host_score_mean,
                    "host_score_stdev": host_score_stdev,
                    "vm_weight_mean": vm_weight_mean,
                    "vm_weight_stdev": vm_weight_stdev,
                    "group_size_mean": group_size_mean,
                    "group_size_stdev": group_size_stdev,
                },
                "policies_file": "synthetic",
                "policy_names": ["cpu-spread", "memory-spread"],
                "aggregates": [UNASSIGNED_TOPIC_MARKER],
            },
            indent=2,
        ),
    )

    # --- Nova ---------------------------------------------------------
    nova_dir = out_dir / "nova"
    nova_dir.mkdir()

    # All hosts belong to the unassigned pool (single aggregate replay).
    (nova_dir / "aggregates.json").write_text(
        json.dumps({UNASSIGNED_TOPIC_MARKER: host_names}, indent=2),
    )

    # Place VMs on hosts roughly evenly.  Order matters for server-group
    # placement decisions below - keep a flat vm->host assignment.
    vms_per_host_target = vms // hosts if hosts else 0
    leftover = vms - vms_per_host_target * hosts

    vm_uuids: list[str] = [str(uuid.uuid4()) for _ in range(vms)]
    vm_to_host: dict[str, str] = {}

    cursor = 0
    for i, host in enumerate(host_names):
        this_hosts_share = vms_per_host_target + (1 if i < leftover else 0)
        for _ in range(this_hosts_share):
            vm_to_host[vm_uuids[cursor]] = host
            cursor += 1

    # Build server groups first so we can annotate Instance.server_groups.
    server_groups: list[dict[str, object]] = []
    vm_to_groups: dict[str, list[str]] = {}

    anti_affinity_share = 0.6
    affinity_share = 0.25
    soft_anti_share = 0.1
    # remaining ~5% → soft-affinity

    for i in range(groups):
        group_id = str(uuid.uuid4())
        roll = rng.random()
        if roll < anti_affinity_share:
            policy = "anti-affinity"
        elif roll < anti_affinity_share + affinity_share:
            policy = "affinity"
        elif roll < anti_affinity_share + affinity_share + soft_anti_share:
            policy = "soft-anti-affinity"
        else:
            policy = "soft-affinity"

        size = _gauss_int(
            rng, group_size_mean, group_size_stdev, low=2, high=max(2, vms),
        )
        if size > vms:
            size = vms

        members = rng.sample(vm_uuids, size)
        for m in members:
            vm_to_groups.setdefault(m, []).append(group_id)

        server_groups.append(
            {
                "id": group_id,
                "name": f"fake-group-{i:05d}",
                "policies": [policy],
                "members": members,
            },
        )

    (nova_dir / "server_groups.json").write_text(
        json.dumps(server_groups, indent=2),
    )

    # Per-host instance lists, matching the dataclass shape of
    # clients.nova.Instance.
    instances: dict[str, list[dict[str, object]]] = {h: [] for h in host_names}
    for idx, vm_uuid in enumerate(vm_uuids):
        host = vm_to_host[vm_uuid]
        flavor_vcpus = rng.choice([1, 2, 4, 8, 16])
        flavor_ram_mb = flavor_vcpus * rng.choice([1024, 2048, 4096])
        instances[host].append(
            {
                "uuid": vm_uuid,
                "name": f"fake-vm-{idx:05d}",
                "internal_name": f"instance-{idx:08x}",
                "host": host,
                "flavor_vcpus": flavor_vcpus,
                "flavor_ram_mb": flavor_ram_mb,
                "status": "ACTIVE",
                "server_groups": vm_to_groups.get(vm_uuid, []),
            },
        )

    (nova_dir / "instances.json").write_text(
        json.dumps(instances, indent=2),
    )

    # All hosts up + enabled.  Replays consult this for destination
    # availability and evacuation candidates; the synthetic fixture
    # mirrors a healthy cluster so the planner has freedom to move VMs.
    services_payload = [
        {
            "host": h,
            "binary": "nova-compute",
            "state": "up",
            "status": "enabled",
            "disabled_reason": "",
            "forced_down": False,
        }
        for h in host_names
    ]
    (nova_dir / "services.json").write_text(
        json.dumps(services_payload, indent=2),
    )

    # --- Prometheus ---------------------------------------------------
    prom_dir = out_dir / "prometheus"
    prom_dir.mkdir()

    # Per-host PromQL imbalance score: normal(mean, stdev) clipped to
    # [0, 1].  Emitted under the `nodename` label to match the sample
    # policies.yaml (host_label: nodename).
    host_cpu_scores = {
        h: _gauss_clipped(
            rng, host_score_mean, host_score_stdev, 0.0, 1.0,
        )
        for h in host_names
    }
    # Memory distribution uses an independent draw so the two policies
    # are meaningfully different inputs.
    host_mem_scores = {
        h: _gauss_clipped(
            rng, host_score_mean, host_score_stdev, 0.0, 1.0,
        )
        for h in host_names
    }

    # Query strings are opaque tokens - the Replay client looks them up
    # by exact string match.  We emit distinctive sentinels so the
    # generated policies.yaml can reference them verbatim.
    cpu_imbalance_q = "FAKE:cpu-imbalance"
    mem_imbalance_q = "FAKE:memory-imbalance"
    cpu_vm_q = "FAKE:cpu-vm-profile"
    mem_vm_q = "FAKE:memory-vm-profile"

    _write_prom_query(
        prom_dir / "imbalance_cpu-spread.json",
        query=cpu_imbalance_q,
        series=host_cpu_scores,
    )
    _write_prom_query(
        prom_dir / "imbalance_memory-spread.json",
        query=mem_imbalance_q,
        series=host_mem_scores,
    )

    # Per-VM weight for each policy.  Label key is the VM UUID, matching
    # vm_profile_label_type: nova_instance_uuid in the synthetic policies.
    vm_cpu_weights = {
        u: _gauss_clipped(
            rng, vm_weight_mean, vm_weight_stdev, 0.001, 1.0,
        )
        for u in vm_uuids
    }
    vm_mem_weights = {
        u: _gauss_clipped(
            rng, vm_weight_mean, vm_weight_stdev, 0.001, 1.0,
        )
        for u in vm_uuids
    }

    _write_prom_query(
        prom_dir / "vm_profile_cpu-spread.json",
        query=cpu_vm_q,
        series=vm_cpu_weights,
    )
    _write_prom_query(
        prom_dir / "vm_profile_memory-spread.json",
        query=mem_vm_q,
        series=vm_mem_weights,
    )

    # --- Matching policies YAML --------------------------------------
    # Written into the snapshot so replays need no external policies
    # file - point kronos.conf's policies_file at this path.
    (out_dir / "policies.yaml").write_text(
        _render_policies_yaml(
            cpu_imbalance_q=cpu_imbalance_q,
            mem_imbalance_q=mem_imbalance_q,
            cpu_vm_q=cpu_vm_q,
            mem_vm_q=mem_vm_q,
        ),
    )


def _render_policies_yaml(
    *,
    cpu_imbalance_q: str,
    mem_imbalance_q: str,
    cpu_vm_q: str,
    mem_vm_q: str,
) -> str:
    return f"""policies:
  - name: cpu-spread
    mode: spread
    weight: 0.3
    imbalance_query: "{cpu_imbalance_q}"
    host_label: host
    vm_profile_query: "{cpu_vm_q}"
    vm_profile_label: instance
    vm_profile_label_type: nova_instance_uuid
    vm_profile_fallback: skip
    threshold: 0.05
    max_migrations_per_cycle: 6

  - name: memory-spread
    mode: spread
    weight: 0.7
    imbalance_query: "{mem_imbalance_q}"
    host_label: host
    vm_profile_query: "{mem_vm_q}"
    vm_profile_label: instance
    vm_profile_label_type: nova_instance_uuid
    vm_profile_fallback: skip
    threshold: 0.10
    max_migrations_per_cycle: 3
"""


def _write_prom_query(
    path: Path,
    *,
    query: str,
    series: dict[str, float],
) -> None:
    path.write_text(
        json.dumps(
            {
                "query": query,
                "health": "healthy",
                "series": series,
                "missing_labels": [],
                "warnings": [],
            },
            indent=2,
        ),
    )


def _cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a synthetic Kronos snapshot for benchmarking.",
    )
    p.add_argument("out_dir", type=Path, help="Directory to create.")
    p.add_argument("--hosts", type=int, default=50)
    p.add_argument("--vms", type=int, default=5000)
    p.add_argument(
        "--groups",
        type=int,
        default=100,
        help="Number of Nova server groups to synthesise.",
    )
    p.add_argument("--host-score-mean", type=float, default=0.35)
    p.add_argument("--host-score-stdev", type=float, default=0.15)
    p.add_argument("--vm-weight-mean", type=float, default=0.01)
    p.add_argument("--vm-weight-stdev", type=float, default=0.005)
    p.add_argument("--group-size-mean", type=float, default=4.0)
    p.add_argument("--group-size-stdev", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def main() -> int:
    args = _cli()
    generate(
        out_dir=args.out_dir,
        hosts=args.hosts,
        vms=args.vms,
        groups=args.groups,
        host_score_mean=args.host_score_mean,
        host_score_stdev=args.host_score_stdev,
        vm_weight_mean=args.vm_weight_mean,
        vm_weight_stdev=args.vm_weight_stdev,
        group_size_mean=args.group_size_mean,
        group_size_stdev=args.group_size_stdev,
        seed=args.seed,
    )
    print(f"Snapshot written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

Configuration reference
=======================

Config split
------------

- ``/etc/kronos/kronos.conf`` - oslo.config INI for daemon settings.
  Everything the daemons need to run: evaluation interval, dry-run,
  aggregate scope, availability zone, cooldowns, messaging transport,
  Prometheus endpoint, Keystone auth.
- ``/etc/kronos/policies.yaml`` - Pydantic-validated YAML describing
  the scheduling policies: PromQL queries, thresholds, weights,
  per-VM profiling queries and fallbacks.

The split is deliberate: policies are a rich, validated document with
cross-field rules (weights summing to 1.0, one mode per file); daemon
settings are flat key-value pairs that fit oslo.config.

Full option reference
---------------------

The complete generated reference - every option of every group,
including the inherited oslo.log and oslo.messaging options - is
checked into the repository at ``docs/configuration/kronos.conf.sample``
and regenerated with:

.. code-block:: console

   oslo-config-generator --config-file etc/oslo-config-generator/kronos.conf

.. literalinclude:: kronos.conf.sample
   :language: ini

Policies file
-------------

See ``etc/kronos/policies.yaml.sample`` in the repository for a
commented example. Load-time invariants:

- policy names are unique;
- all policies in one file share a ``mode`` (``spread`` or ``pack``);
- enabled policy ``weight`` values sum to 1.0;
- ``imbalance_query`` must return per-host values in [0, 1] - enforced
  at runtime by the scorer, which skips the policy for the cycle (with
  an error logged) on out-of-range data.


CPU compatibility
-----------------

Set ``[engine] require_cpu_compatibility = true`` to require a
destination's Placement ``HW_CPU_*`` traits to contain every CPU trait
reported for the source host. The default is false. This option is
independent of ``enforce_placement_claims``.

The shared constraint checker applies this rule to spread, pack,
disabled-host evacuation, and affinity repair. Checks run after host
availability and capacity claims, before server-group checks. A DEBUG
message names the rejected host pair and missing traits.

The engine lists Placement providers and reads traits once per in-scope
host per cycle, deduplicating overlapping aggregates and filtering to
the configured availability zone. The service user needs
``placement:resource_providers:list`` and
``placement:resource_providers:traits:list`` access. The traits endpoint
uses Placement microversion 1.6. Provider names must match the Nova host
names used by the engine. Missing source or destination data blocks a
move. Any traits fetch failure blocks all moves for that cycle.

A successful response containing no ``HW_CPU_*`` traits is different
from unavailable data. An empty source set permits moves to destinations
with known data and logs a warning once per cycle that the check is
blind for that source.

This is a host-level approximation, not a per-instance CPU model check.
Nova's libvirt driver reports host features for host-model and
host-passthrough, and a union of configured models for custom mode.
The prefix also includes ``HW_CPU_HYPERTHREADING``, so the check can
reject moves for more than instruction-set differences. It can reject
moves Nova would accept and miss incompatibilities involving unmapped
flags or guests retaining an older CPU model after reconfiguration.
Nova's migration pre-check remains authoritative.

Producer source:
`Nova libvirt CPU trait reporting <https://github.com/openstack/nova/blob/stable/2025.2/nova/virt/libvirt/driver.py>`_.
API source:
`Placement provider traits <https://github.com/openstack/placement/blob/stable/2025.2/placement/handlers/trait.py>`_.

When the option is enabled, both ``kronos-record`` and engine SIGUSR1
snapshots write ``placement/cpu_traits.json``, a mapping from host names
to sorted trait lists. Failed collection writes an empty mapping.
Replay reads this file without contacting Placement. Missing files in
older snapshots fail closed when CPU checking is enabled.

For CPU compatibility replay, enable ``require_cpu_compatibility`` and
set ``enforce_placement_claims = false``. Capacity claims are not
recorded by the existing snapshot format. Leaving that gate enabled
pauses replay planning with an explicit error in the logs.

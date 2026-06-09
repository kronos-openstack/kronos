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

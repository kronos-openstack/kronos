Operator runbook
================

First deployment
----------------

1. Create the service user in Keystone and grant it admin on the
   service project (it must see hypervisors, instances, server groups
   and compute services across projects, read Placement, and - for the
   executor - live-migrate servers).
2. Write ``kronos.conf`` and ``policies.yaml``; validate with
   ``kronos-test-config``.
3. Start executors for every aggregate the engine will manage.
4. Start the engine with ``[engine] dry_run = true``. Each cycle logs
   a CycleReport: per-policy imbalance per aggregate, and the plans it
   *would* emit. Watch a few cycles; tune thresholds and weights until
   plans look sensible.
5. Set ``dry_run = false``; restart the engine.

Conservative first settings: keep
``max_migrations_per_cycle`` low (1-3), leave the placement gate on
(``enforce_placement_claims = true``, default), leave the disk check
off unless ephemeral storage is host-local
(``enforce_placement_disk = false``, default).

Upgrades
--------

The two daemons share no persistent state - all state is rebuilt from
Nova, Placement and Prometheus every cycle, and cooldown/quarantine
state lives in engine memory only (lost on restart; worst case the
engine re-plans something it would have skipped, and the executor's
pre-flight catches anything stale).

1. Upgrade and restart executors first (they only consume tasks).
2. Upgrade and restart engines.

venv install: ``pip install -U kronos-openstack`` then restart the
units. Kolla image: build the new image, then restart containers with
the new tag. There are no database migrations, ever.

Rollback is the same procedure with the previous version.

Failure modes
-------------

Engine-side, per cycle (all of these self-heal next cycle):

Prometheus unreachable
   The scorer fails the policy query; the policy is skipped for the
   cycle and an error is logged. With all policies skipped no planning
   happens. Look for ``PrometheusUnreachableError`` or the policy
   skip reason in the engine log.

Prometheus partial / stale data
   Hosts missing from query results are reported as missing labels
   (the engine passes the expected host list to every query). VMs
   without profile data follow the policy's ``vm_profile_fallback``
   (default ``skip``: the VM is excluded from planning).

Nova os-services fetch fails
   The host-availability gate fails closed: an empty service map is
   installed and every destination is rejected, so no migrations are
   emitted that cycle. Log line mentions installing an empty service
   map after the fetch error.

Placement API unreachable
   The claims gate fails closed the same way: empty snapshot, every
   destination rejected, planning effectively paused. Fix the
   placement endpoint; no Kronos restart needed.

Migration failures (executor notifications)
   The engine's result listener dispatches on ``error_type``:

   - ``PreFlightError`` / ``MigrationFailed`` / ``MigrationTimeout``
     after final retry: the VM is quarantined for
     ``[engine] instance_quarantine_seconds`` (default 3600; ``-1``
     means indefinite).
   - ``NovaClientError``: treated as transient infrastructure trouble;
     no quarantine.
   - ``PlacementRejected``: capacity raced away between plan and
     execute; no quarantine, expected to clear next cycle.
   - Unknown ``error_type``: defensive quarantine.

Executor restart
   Tasks queued in the local scheduler are lost (RPC messages are
   acked on submission). Nova continues migrations it already
   started. The engine re-evaluates next cycle. No action needed.

Reading the logs
----------------

Engine (per cycle):

- The CycleReport logs each aggregate's per-policy imbalance, whether
  imbalance was detected, and the emitted plan (or why none was).
- Rejected candidate moves are logged at DEBUG with the reason
  (service state, server-group rule, placement headroom, AZ filter,
  threshold). Run with ``debug = true`` while tuning.
- Cooldown skips are logged with the remaining seconds.

Executor (per task):

- Pre-flight failures name the check that refused (instance not
  ACTIVE, instance moved, source/destination service state).
- Each retry logs the attempt count and backoff; after
  ``max_retries`` the task is dropped with a final log line and the
  failure notification carries the ``error_type`` shown above.

Diagnosing "Kronos isn't migrating anything"
--------------------------------------------

In order of likelihood:

1. ``dry_run`` still true.
2. No policy crosses its ``threshold`` (check the CycleReport
   imbalance values).
3. Aggregate or instances cooling down / quarantined (INFO log).
4. Placement gate or service gate failing closed (see above) - check
   for the fetch errors.
5. All candidate moves rejected by constraints - run at DEBUG and read
   the per-candidate rejection reasons.
6. Hosts filtered out by the AZ scope: hosts whose nova-compute
   reports a different zone than ``[engine] availability_zone`` are
   dropped from every aggregate.

On-demand snapshots
-------------------

With ``[engine] snapshot_dir`` set, ``SIGUSR1`` captures a full
Nova + Prometheus snapshot in ``kronos-record`` format. Feed it to
``kronos-replay`` to reproduce a planning decision offline (optionally
seeding cooldown state via ``cooldowns.json``), or attach it to a bug
report.

Kronos
======

Kronos is a PromQL-driven VM placement engine for OpenStack. It
evaluates Prometheus metrics per Nova host aggregate and plans live
migrations to balance (spread) or consolidate (pack) workloads, with
server-group affinity enforcement, disabled-host evacuation, placement
claims checking, and cooldown/quarantine safety rails.

It is split into two daemons connected by oslo.messaging:

- ``kronos-engine`` - evaluates policies on an interval, plans
  migrations, casts tasks over RPC. One engine per availability zone.
- ``kronos-executor`` - consumes tasks, runs pre-flight checks, calls
  Nova live-migrate, reports results as notifications. Services one or
  more aggregates per process.

Supporting tools: ``kronos-test-config`` (validate configs),
``kronos-record`` (snapshot live cluster state), ``kronos-replay``
(run the engine offline against a snapshot).

.. toctree::
   :maxdepth: 2

   installation
   configuration/index
   deployment/systemd
   deployment/container
   operations/runbook

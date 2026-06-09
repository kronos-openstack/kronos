Deploying with systemd
======================

Unit files live in ``etc/systemd/`` in the repository. Both are
instanced units (``name@instance.service``).

Setup
-----

.. code-block:: console

   # service user
   useradd --system --home-dir /var/lib/kronos --create-home kronos

   # install kronos into a venv and link the binaries
   python3 -m venv /opt/kronos
   /opt/kronos/bin/pip install kronos-openstack
   ln -s /opt/kronos/bin/kronos-engine /usr/bin/kronos-engine
   ln -s /opt/kronos/bin/kronos-executor /usr/bin/kronos-executor

   # units
   cp etc/systemd/kronos-engine@.service /etc/systemd/system/
   cp etc/systemd/kronos-executor@.service /etc/systemd/system/
   systemctl daemon-reload

Engine
------

One engine per scope - typically one per availability zone. The
instance name selects the config file
(``/etc/kronos/kronos-<instance>.conf``):

.. code-block:: console

   cp kronos.conf /etc/kronos/kronos-default.conf
   systemctl enable --now kronos-engine@default.service

An engine for a second AZ is just another instance with its own
config file (different ``[engine] availability_zone`` and
``aggregates``):

.. code-block:: console

   systemctl enable --now kronos-engine@az2.service

On-demand snapshots (requires ``[engine] snapshot_dir`` to be set, and
that directory added to ``ReadWritePaths`` in a drop-in because the
unit runs with ``ProtectSystem=strict``):

.. code-block:: console

   systemctl kill -s SIGUSR1 kronos-engine@default.service

Executor
--------

The instance name is the Nova host aggregate to service:

.. code-block:: console

   systemctl enable --now kronos-executor@gpu-aggregate.service
   systemctl enable --now kronos-executor@hpc-aggregate.service

One executor process can also service several aggregates and/or the
unassigned-hosts pool (``--aggregate`` is repeatable, plus
``--unassigned``); for that, override ``ExecStart`` in a drop-in
instead of starting several instances. Per-aggregate concurrency,
isolation and cooldown behaviour are identical either way.

Order of operations
-------------------

1. Validate configs: ``kronos-test-config --config-file ...``
2. Start executors for every aggregate in the engine's scope.
3. Start the engine with ``[engine] dry_run = true`` and watch one or
   two cycles in the journal.
4. Set ``dry_run = false`` and restart the engine instance.

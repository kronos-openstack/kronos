Installation
============

From PyPI
---------

Kronos requires Python 3.12 or newer.

.. code-block:: console

   python3 -m venv /opt/kronos
   /opt/kronos/bin/pip install kronos-openstack
   /opt/kronos/bin/kronos-engine --help

Five console scripts are installed: ``kronos-engine``,
``kronos-executor``, ``kronos-test-config``, ``kronos-record``,
``kronos-replay``.

From source
-----------

.. code-block:: console

   git clone https://github.com/kronos-openstack/kronos
   cd kronos
   python3 -m venv .venv
   .venv/bin/pip install .

Configuration files
-------------------

Kronos reads two files, by default under ``/etc/kronos``:

- ``kronos.conf`` - oslo.config INI with daemon settings (intervals,
  auth, messaging transport). Quickstart sample:
  ``etc/kronos/kronos.conf.sample``.
- ``policies.yaml`` - the scheduling policies (PromQL queries,
  thresholds, weights). Sample: ``etc/kronos/policies.yaml.sample``.

Validate both before starting anything:

.. code-block:: console

   kronos-test-config --config-file /etc/kronos/kronos.conf

Keystone credentials
--------------------

The engine and executor authenticate against Keystone with the
``[nova]`` config group (keystoneauth1 semantics). The service user
needs enough privilege to list hypervisors, instances, server groups
and compute services across projects, to read the Placement API
(``placement:resource_providers:list`` plus the per-provider
inventories/usages rules), and - executor only - to live-migrate
servers. In practice this means the ``admin`` role on the service
project on most clouds.

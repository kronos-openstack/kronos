Deploying with containers (Kolla)
=================================

The image follows the Kolla runtime contract, so it drops into a
Kolla-Ansible deployment the same way every other OpenStack service
container does, and can equally run standalone under plain Docker or
Podman.

Image
-----

``docker/Dockerfile`` extends ``quay.io/openstack.kolla/openstack-base``
and installs Kronos into the Kolla virtualenv against the
upper-constraints of the target release, so oslo and OpenStack client
versions match the rest of the deployment. One image serves both
daemons: the ``command`` in ``config.json`` selects which one starts.

.. code-block:: console

   python -m build
   docker build -f docker/Dockerfile \
     --build-arg BASE_TAG=2025.1-ubuntu-noble \
     -t kronos:2025.1-ubuntu-noble .

``BASE_TAG`` selects the OpenStack release and distro of the base
image; use the same release your cloud runs.

Runtime contract
----------------

At start, ``kolla_start``:

1. runs ``kolla_set_configs`` (as root, via the sudoers grant on the
   ``kolla`` group), which reads
   ``/var/lib/kolla/config_files/config.json``, copies each listed
   config file into place with the declared owner and permissions, and
   writes the command to ``/run_command``;
2. execs that command as the ``kronos`` user.

Example ``config.json`` files are in ``etc/kolla/``:
``kronos-engine.json.example`` and ``kronos-executor.json.example``.

Standalone run
--------------

.. code-block:: console

   docker run -d --name kronos-engine \
     -e KOLLA_CONFIG_STRATEGY=COPY_ALWAYS \
     -v /etc/kronos/kronos-engine.json:/var/lib/kolla/config_files/config.json:ro \
     -v /etc/kronos/kronos.conf:/var/lib/kolla/config_files/kronos.conf:ro \
     -v /etc/kronos/policies.yaml:/var/lib/kolla/config_files/policies.yaml:ro \
     kronos:2025.1-ubuntu-noble

The executor is the same image with the executor ``config.json``
(its ``command`` carries the ``--aggregate`` flags).

With kolla-ansible
------------------

Treat Kronos as a custom service: ship the image to your registry,
template ``config.json`` and ``kronos.conf`` like any other service's
config files, and run one engine container per availability zone plus
executor containers covering every aggregate in scope. No HA
considerations beyond systemd-style restarts: the engine re-plans
every cycle from fresh state, and executors ack RPC messages on
submission, so a restarted container simply picks up new casts.

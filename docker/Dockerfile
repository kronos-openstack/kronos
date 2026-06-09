# Kronos service image.
#
# Extends the Kolla openstack-base image and follows the same runtime
# contract as every Kolla service container:
#
# - kronos is installed into the Kolla virtualenv (/var/lib/kolla/venv)
#   against the upper-constraints of the target OpenStack release, so
#   the oslo / OpenStack client versions match the rest of the
#   deployment.
# - The container starts via kolla_start, which reads
#   /var/lib/kolla/config_files/config.json (mounted by the operator or
#   by kolla-ansible), copies the listed config files into place with
#   the right ownership, and execs the "command" defined there.
# - The same image runs the engine and the executor: the command in
#   config.json selects which daemon starts. See
#   etc/kolla/kronos-engine.json.example and
#   etc/kolla/kronos-executor.json.example.
#
# Build (wheel first, then image; run from the repo root):
#
#   python -m build
#   docker build -f docker/Dockerfile \
#     --build-arg BASE_TAG=2025.1-ubuntu-noble \
#     -t kronos:2025.1-ubuntu-noble .
#
# Run (standalone, without kolla-ansible):
#
#   docker run -d \
#     -e KOLLA_CONFIG_STRATEGY=COPY_ALWAYS \
#     -v /etc/kronos/kronos-engine.json:/var/lib/kolla/config_files/config.json:ro \
#     -v /etc/kronos/kronos.conf:/var/lib/kolla/config_files/kronos.conf:ro \
#     -v /etc/kronos/policies.yaml:/var/lib/kolla/config_files/policies.yaml:ro \
#     kronos:2025.1-ubuntu-noble

ARG BASE_IMAGE=quay.io/openstack.kolla/openstack-base
ARG BASE_TAG=2025.1-ubuntu-noble
FROM ${BASE_IMAGE}:${BASE_TAG}

LABEL maintainer="Kronos maintainers" name="kronos"

# Kolla creates service users in the base image from its own users
# table; kronos is not in that table, so create the user here. The uid
# sits at the top of Kolla's reserved 42400-42499 range to avoid
# colliding with upstream service users. Membership in the kolla group
# is what grants the sudoers entries kolla_start relies on
# (kolla_set_configs and friends run via sudo).
RUN groupadd --gid 42499 kronos \
    && useradd --uid 42499 --gid kronos --home-dir /var/lib/kronos \
        --create-home --shell /usr/sbin/nologin kronos \
    && usermod --append --groups kolla kronos \
    && mkdir -p /etc/kronos \
    && chown kronos:kronos /etc/kronos

# Install the wheel built by `python -m build`. python3 here is the
# Kolla venv python (first in PATH), so this lands in the venv and the
# kronos-engine / kronos-executor entry points appear on PATH.
# The base image bakes in opendev's CI-internal pip mirror via
# PIP_INDEX_URL / PIP_EXTRA_INDEX_URL env vars, unreachable outside
# their build infra; point this install at PyPI instead.
COPY dist/*.whl /tmp/kronos-dist/
RUN PIP_INDEX_URL=https://pypi.org/simple \
    PIP_EXTRA_INDEX_URL= \
    PIP_TRUSTED_HOST= \
    python3 -m pip --no-cache-dir install \
        -c /requirements/upper-constraints.txt \
        /tmp/kronos-dist/*.whl \
    && rm -rf /tmp/kronos-dist

# Reference samples; the real config arrives via config.json at start.
COPY etc/kronos/kronos.conf.sample etc/kronos/policies.yaml.sample /etc/kronos/

CMD ["kolla_start"]

USER kronos
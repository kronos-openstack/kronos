"""oslo.config option definitions for Kronos daemon configuration.

Nova/OpenStack authentication uses keystoneauth1 loading, which
automatically registers auth_type, auth_url, username, password,
project_name, etc. under the ``[nova]`` config group.
"""

from keystoneauth1 import loading as ks_loading
from oslo_config import cfg

engine_opts = [
    cfg.IntOpt(
        "evaluation_interval",
        default=60,
        min=5,
        help="Seconds between policy evaluation cycles.",
    ),
    cfg.BoolOpt(
        "dry_run",
        default=True,
        help="When true, log migration plans without executing them.",
    ),
    cfg.StrOpt(
        "policies_file",
        default="/etc/kronos/policies.yaml",
        help="Path to the policies YAML file.",
    ),
]

prometheus_opts = [
    cfg.URIOpt(
        "url",
        schemes=["http", "https"],
        help="Prometheus base URL (e.g. http://prometheus:9090).",
    ),
    cfg.IntOpt(
        "timeout",
        default=30,
        min=1,
        help="HTTP request timeout in seconds.",
    ),
    cfg.IntOpt(
        "max_retries",
        default=3,
        min=0,
        help="Maximum number of retries for failed queries.",
    ),
    cfg.FloatOpt(
        "retry_backoff",
        default=1.0,
        min=0.1,
        help="Base backoff time in seconds between retries.",
    ),
    cfg.IntOpt(
        "staleness_threshold",
        default=300,
        min=10,
        help="Maximum sample age in seconds before data is considered stale.",
    ),
    cfg.BoolOpt(
        "verify_ssl",
        default=True,
        help="Verify SSL certificates when connecting to Prometheus.",
    ),
    cfg.StrOpt(
        "ca_cert",
        help="Path to CA certificate bundle for Prometheus TLS.",
    ),
    cfg.StrOpt(
        "bearer_token",
        secret=True,
        help="Bearer token for Prometheus authentication.",
    ),
    cfg.StrOpt(
        "bearer_token_file",
        help="Path to file containing bearer token for Prometheus authentication.",
    ),
]

ENGINE_GROUP = "engine"
PROMETHEUS_GROUP = "prometheus"
NOVA_GROUP = "nova"


def register_opts(conf: cfg.ConfigOpts) -> None:
    """Register all Kronos configuration option groups."""
    conf.register_opts(engine_opts, group=ENGINE_GROUP)
    conf.register_opts(prometheus_opts, group=PROMETHEUS_GROUP)

    # Nova auth via keystoneauth1 — registers auth_type, auth_url,
    # username, password, project_name, user_domain_name, etc.
    ks_loading.register_auth_conf_options(conf, NOVA_GROUP)
    ks_loading.register_session_conf_options(conf, NOVA_GROUP)
    ks_loading.register_adapter_conf_options(conf, NOVA_GROUP)


def list_opts() -> list[tuple[str, list[cfg.Opt]]]:
    """Return a list of (group, opts) for oslo.config sample generation."""
    return [
        (ENGINE_GROUP, engine_opts),
        (PROMETHEUS_GROUP, prometheus_opts),
        # keystoneauth1 opts are auto-registered; include for documentation
        (NOVA_GROUP, (
            ks_loading.get_auth_common_conf_options()
            + ks_loading.get_session_conf_options()
            + ks_loading.get_adapter_conf_options()
        )),
    ]

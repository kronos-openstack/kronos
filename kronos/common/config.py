"""oslo.config option definitions for Kronos daemon configuration.

Nova/OpenStack authentication uses keystoneauth1 loading, which
automatically registers auth_type, auth_url, username, password,
project_name, etc. under the ``[nova]`` config group.
"""

from keystoneauth1 import loading as ks_loading
from oslo_config import cfg

messaging_opts = [
    cfg.StrOpt(
        "transport_url",
        help="oslo.messaging transport URL (e.g. rabbit://guest:guest@localhost:5672/).",
    ),
]

executor_opts = [
    cfg.IntOpt(
        "max_concurrent_migrations",
        default=2,
        min=1,
        max=10,
        help="Maximum simultaneous live migrations per executor.",
    ),
    cfg.IntOpt(
        "migration_timeout",
        default=600,
        min=60,
        help="Timeout in seconds for a single live migration.",
    ),
    cfg.IntOpt(
        "migration_poll_interval",
        default=10,
        min=5,
        help="Seconds between Nova migration status polls.",
    ),
    cfg.IntOpt(
        "max_retries",
        default=3,
        min=0,
        help="Maximum retry attempts for failed migrations.",
    ),
    cfg.FloatOpt(
        "retry_backoff",
        default=30.0,
        min=1.0,
        help="Base backoff in seconds between retries.",
    ),
    cfg.IntOpt(
        "stagger_seconds",
        default=30,
        min=0,
        help="Delay between consecutive migration starts.",
    ),
]

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
    cfg.IntOpt(
        "instance_cooldown",
        default=900,
        help="Seconds after a VM is included in a plan before it can be re-planned.",
    ),
    cfg.IntOpt(
        "instance_quarantine_seconds",
        default=3600,
        min=-1,
        help=(
            "Seconds to skip a VM after its migration definitively failed "
            "(retries exhausted). Use -1 to quarantine indefinitely. "
            "Applied on PreFlightError, MigrationFailed, and MigrationTimeout."
        ),
    ),
    cfg.IntOpt(
        "cooldown",
        default=600,
        help=(
            "Seconds after a plan is emitted for an aggregate before another "
            "plan can be emitted for the same aggregate."
        ),
    ),
    cfg.ListOpt(
        "aggregates",
        default=[],
        help=(
            "Nova host aggregate names this engine manages. Each listed "
            "aggregate is planned independently; migrations stay within "
            "aggregate boundaries."
        ),
    ),
    cfg.BoolOpt(
        "include_unassigned_hosts",
        default=True,
        help=(
            "Also manage compute hosts that belong to no Nova host aggregate. "
            "In Nova, aggregate membership is optional — hosts outside any "
            "aggregate form an ungrouped pool. With this enabled, the engine "
            "treats that pool as an independent planning scope: migrations "
            "stay within the pool and never cross into a named aggregate."
        ),
    ),
    cfg.BoolOpt(
        "enforce_hard_affinity",
        default=False,
        help=(
            "Emit enforcement migrations for Nova server groups with the "
            "'affinity' or 'anti-affinity' policy when the current "
            "placement violates the rule. Destinations are chosen to "
            "minimise the combined policy imbalance from policies.yaml, "
            "never violating the policy imbalance threshold. "
            "Enforcement moves share max_migrations_per_cycle with the "
            "imbalance planner."
        ),
    ),
    cfg.BoolOpt(
        "enforce_soft_affinity",
        default=False,
        help=(
            "Emit enforcement migrations for Nova server groups with the "
            "'soft-affinity' or 'soft-anti-affinity' policy when the "
            "current placement violates the rule. Destinations are "
            "chosen to minimise the combined policy imbalance from "
            "policies.yaml, never violating the policy imbalance "
            "threshold. Soft rules are best-effort at Nova placement "
            "time; enable this only if you want Kronos to actively "
            "fight the Nova scheduler when soft placement hints cannot "
            "be honoured."
        ),
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

MESSAGING_GROUP = "messaging"
EXECUTOR_GROUP = "executor"
ENGINE_GROUP = "engine"
PROMETHEUS_GROUP = "prometheus"
NOVA_GROUP = "nova"


def register_opts(conf: cfg.ConfigOpts) -> None:
    """Register all Kronos configuration option groups."""
    conf.register_opts(messaging_opts, group=MESSAGING_GROUP)
    conf.register_opts(executor_opts, group=EXECUTOR_GROUP)
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
        (MESSAGING_GROUP, messaging_opts),
        (EXECUTOR_GROUP, executor_opts),
        (ENGINE_GROUP, engine_opts),
        (PROMETHEUS_GROUP, prometheus_opts),
        # keystoneauth1 opts are auto-registered; include for documentation
        (NOVA_GROUP, (
            ks_loading.get_auth_common_conf_options()
            + ks_loading.get_session_conf_options()
            + ks_loading.get_adapter_conf_options()
        )),
    ]

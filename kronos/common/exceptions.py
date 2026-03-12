"""Kronos exception hierarchy.

Follows the standard OpenStack exception pattern (Nova, Neutron, Heat):
each exception defines a ``msg_fmt`` with %(placeholder)s formatting,
and an optional HTTP ``code`` for API responses.
"""


class KronosException(Exception):
    """Base exception for all Kronos errors.

    To correctly use this class, inherit from it and define a ``msg_fmt``
    property. That ``msg_fmt`` will get printf'd with the keyword arguments
    provided to the constructor.

    .. code-block:: python

        class PolicyNotFound(KronosException):
            msg_fmt = "Policy '%(name)s' not found."

        raise PolicyNotFound(name="cpu_spread")
    """

    msg_fmt = "An unknown exception occurred."
    code = 500

    def __init__(self, message: str | None = None, **kwargs: object) -> None:
        self.kwargs = kwargs
        if message is None:
            try:
                message = self.msg_fmt % kwargs
            except (KeyError, TypeError):
                message = self.msg_fmt
        self.message = message
        super().__init__(message)


# --- Configuration ---

class ConfigurationError(KronosException):
    msg_fmt = "Configuration error: %(reason)s"


class PolicyFileNotFound(ConfigurationError):
    msg_fmt = "Policy file not found: %(path)s"


class PolicyValidationError(ConfigurationError):
    msg_fmt = "Policy validation failed: %(reason)s"


# --- Prometheus ---

class PrometheusError(KronosException):
    msg_fmt = "Prometheus error: %(reason)s"


class PrometheusUnreachableError(PrometheusError):
    msg_fmt = "Prometheus server unreachable at %(url)s: %(reason)s"


class PrometheusStalenessError(PrometheusError):
    msg_fmt = (
        "Prometheus data is stale: sample age %(age_seconds)s seconds "
        "exceeds threshold %(threshold_seconds)s seconds."
    )


class PrometheusPartialDataError(PrometheusError):
    msg_fmt = (
        "Prometheus returned incomplete data: missing %(missing_count)s "
        "of %(expected_count)s expected series."
    )

    def __init__(self, message: str | None = None, **kwargs: object) -> None:
        self.missing_labels: set[str] = set()
        if "missing_labels" in kwargs:
            missing = kwargs.pop("missing_labels")
            if isinstance(missing, set):
                self.missing_labels = missing
            kwargs["missing_count"] = len(self.missing_labels)
        super().__init__(message, **kwargs)


class PrometheusQueryError(PrometheusError):
    msg_fmt = "PromQL query failed: %(reason)s"


# --- Nova / OpenStack ---

class NovaClientError(KronosException):
    msg_fmt = "Nova API error: %(reason)s"


class AggregateNotFound(NovaClientError):
    msg_fmt = "Host aggregate '%(aggregate)s' not found."
    code = 404


class HostNotFound(NovaClientError):
    msg_fmt = "Compute host '%(host)s' not found."
    code = 404


# --- Engine ---

class PolicyEvaluationError(KronosException):
    msg_fmt = "Policy '%(policy_name)s' evaluation failed: %(reason)s"


class ImbalanceDetectionError(PolicyEvaluationError):
    msg_fmt = (
        "Imbalance detection failed for policy '%(policy_name)s': %(reason)s"
    )

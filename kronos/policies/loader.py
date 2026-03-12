"""Load and validate policy YAML configuration files."""

from __future__ import annotations

from pathlib import Path

import yaml
from oslo_log import log as logging
from pydantic import ValidationError

from kronos.common.exceptions import PolicyFileNotFound, PolicyValidationError
from kronos.policies.models import PoliciesConfig

LOG = logging.getLogger(__name__)


def load_policies(path: str | Path) -> PoliciesConfig:
    """Load policies from a YAML file.

    :param path: Path to the policies YAML file.
    :returns: Validated PoliciesConfig instance.
    :raises PolicyFileNotFound: If the file does not exist.
    :raises PolicyValidationError: If the YAML is invalid or fails validation.
    """
    path = Path(path)

    if not path.is_file():
        raise PolicyFileNotFound(path=str(path))

    LOG.info("Loading policies from %s", path)

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PolicyValidationError(reason=f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyValidationError(
            reason=f"Expected a YAML mapping at top level, got {type(raw).__name__}."
        )

    try:
        config = PoliciesConfig.model_validate(raw)
    except ValidationError as exc:
        raise PolicyValidationError(reason=str(exc)) from exc

    enabled_count = sum(1 for p in config.policies if p.enabled)
    LOG.info(
        "Loaded %d policies (%d enabled) from %s",
        len(config.policies),
        enabled_count,
        path,
    )

    return config

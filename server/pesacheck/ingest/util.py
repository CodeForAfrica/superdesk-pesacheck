import logging
import os

logger = logging.getLogger(__name__)


def env_float(name, default):
    """Read a float tuning knob from the environment, falling back to default.

    A malformed value is ignored (with a warning) rather than crashing ingest.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using default %s", name, raw, default)
        return default


def env_int(name, default):
    return int(env_float(name, default))

from .medium_parser import MediumParser  # noqa
from .ghost_parser import GhostParser  # noqa
from .ghost_feeding_service import GhostFeedingService  # noqa
from . import ghost_profile


def init_app(app) -> None:
    """Initialize custom ingest app"""

    # Inert unless GHOST_INGEST_PROFILE is set. Installed here rather than at
    # module import so it also wraps in the celery worker, which builds the full
    # app via INSTALLED_APPS.
    ghost_profile.install()

"""Silence superdesk-core's per-EXIF-tag debug prints during image ingest.

Background (investigated 2026-08-31):
``superdesk/media/image.py`` ``get_meta`` walks every EXIF tag on an image and
emits five ``print()`` calls in the process — ``Attempting exif key <k>, val
<v>``, ``\\tKey not found``, ``\\tUpdated key = <key>``, ``\\tKey is for GPS
Info`` and the raw GPS ``value``. These are leftover debug scaffolding, not
logging: they go to stdout, not through ``logging``.

Under Celery this is worse than harmless. ``worker_redirect_stdouts_level``
defaults to ``WARNING``, so the worker captures task stdout and re-emits every
line as a **WARNING** log record. During Ghost ingest — one ``get_meta`` per
image, one line per EXIF tag — this is the single largest source of log volume
reaching Grafana Cloud, and because it surfaces at WARN, lowering the log level
does not remove it. It carries no signal beyond "an image is being processed".

``get_meta`` has no non-print output path we can toggle and core is pinned to a
moving ``@develop`` ref (never edit ``site-packages``), so we wrap it and run
the original with stdout redirected to a discard sink. Only ``get_meta``'s own
prints are swallowed — the redirect is scoped to that single call — and the
extracted metadata (the return value) is untouched.

``media_operations.py`` binds the function at import time
(``from .image import get_meta``) and calls that local reference, so both the
source ``superdesk.media.image.get_meta`` and the already-imported
``superdesk.media.media_operations.get_meta`` are rebound.

Thread-safety note: ``redirect_stdout`` swaps the process-global ``sys.stdout``
for the duration of the call, so a concurrent thread printing *during* a
``get_meta`` call would also be swallowed. In the worker every ``print`` is
itself redirected debug noise, and the window is a single metadata read, so the
trade is acceptable. There is a matching Alloy drop-stage (observability.go) as
a second net for anything that slips through (e.g. after a core bump moves the
prints, or another process calls ``get_meta``).

Applied once from ``app.get_app``. Invalidated if core stops ``print``-ing in
``get_meta`` (then this becomes a no-op wrapper and can be dropped) or moves the
function out of ``superdesk.media.image``.
"""

import contextlib
import logging

logger = logging.getLogger(__name__)


class _NullSink:
    """A write sink that discards everything, reused across all calls.

    Cheaper than a fresh ``io.StringIO`` per call — it neither allocates nor
    buffers the swallowed text (an image with many EXIF tags would otherwise
    accumulate every discarded line until the call returns).
    """

    def write(self, *_args):
        return 0

    def flush(self):
        pass


_NULL_SINK = _NullSink()


def _quiet(get_meta):
    """Wrap get_meta so its stdout debug prints are discarded."""

    def wrapper(file_stream):
        with contextlib.redirect_stdout(_NULL_SINK):
            return get_meta(file_stream)

    wrapper._pesacheck_exif_quiet = True
    return wrapper


def apply():
    """Idempotently rebind get_meta to a stdout-swallowing wrapper."""
    try:
        from superdesk.media import image as image_module
        from superdesk.media import media_operations
    except Exception as exc:
        logger.error(
            "exif_log_patch: could not import superdesk.media, not patching: %r", exc
        )
        return

    if getattr(image_module.get_meta, "_pesacheck_exif_quiet", False):
        return

    quiet = _quiet(image_module.get_meta)
    image_module.get_meta = quiet
    # media_operations did `from .image import get_meta`, so it holds its own
    # reference (the one process_image actually calls) that must be rebound too.
    media_operations.get_meta = quiet

    logger.info("exif_log_patch: get_meta EXIF debug prints silenced")

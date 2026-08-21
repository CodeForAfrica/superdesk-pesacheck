"""Give image downloads HTTP keep-alive.

Same shape as the other patches here (``media_patch``, ``publish_patch``,
``celery_eager_patch``): a defect in pinned superdesk-core that we cannot edit.
Unlike ``publish_patch`` this one does not copy a core function body — it wraps
the original and delegates, so a core bump changes behaviour here only if the
signature changes.

THE PROBLEM
-----------
``media_operations.download_file_from_url`` builds a fresh session per call::

    if session is None:
        session = requests.Session()

and no caller in the ingest path passes one (``renditions.py`` calls it as
``download_file_from_url(url, kwargs)``). So every image costs a new TCP connect
and TLS handshake — roughly 150-400ms against a CDN, against a measured ~5.3s
average download. Over a backfill of ~92,000 images that is hours of aggregate
thread time thrown away for nothing, and a fresh connection per request is also
the traffic shape a CDN is most likely to treat as abuse.

WHAT THIS DOES
--------------
Supplies a session so the original does not have to invent one. Sessions are
thread-local because ``requests.Session`` is not thread-safe and images are
fetched from a thread pool: one session per worker thread, created on first use
and kept for the life of the thread, giving keep-alive and urllib3 connection
pooling without sharing state across threads. The pool is fixed-size and
long-lived, so this cannot grow without bound.

Nothing else changes: same arguments, same return value, same exceptions. A
caller that passes its own session keeps it.

NOT ERROR CLASSIFICATION
------------------------
Recovering the HTTP status of a failed download is a separate concern and does
*not* live here — core discards the response before raising, so reading the
status needs the response itself. ``ghost_parser._classify_response`` does that
with a ``requests`` response hook passed through ``request_kwargs``, which needs
no patch at all. Resist the temptation to move it here.
"""

import logging
import threading

import requests

logger = logging.getLogger(__name__)

_local = threading.local()


def _session():
    """This thread's session, created on first use."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        _local.session = session
    return session


def apply():
    try:
        from superdesk.media import media_operations, renditions
    except ImportError:
        logger.error("Could not import superdesk.media; not patching image fetch")
        return

    if getattr(media_operations, "_pesacheck_image_fetch_patched", False):
        return

    original = media_operations.download_file_from_url

    def download_file_from_url(url, request_kwargs=None, session=None):
        return original(url, request_kwargs, session or _session())

    media_operations.download_file_from_url = download_file_from_url
    # renditions.py did ``from .media_operations import download_file_from_url``,
    # binding its own reference at import time, and that copy is the one the
    # ingest path calls. Rebinding only media_operations would leave it on the
    # original. Anything else that took the same import keeps the unwrapped
    # function and simply does not get keep-alive — a missed optimisation, not a
    # behaviour change, which is why the list does not need to be exhaustive.
    renditions.download_file_from_url = download_file_from_url
    media_operations._pesacheck_image_fetch_patched = True
    logger.info("Patched download_file_from_url for per-thread connection reuse")

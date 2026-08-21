"""Opt-in timing instrumentation for Ghost ingest.

Off unless ``GHOST_INGEST_PROFILE`` is truthy: ``install()`` returns immediately
and nothing is wrapped.

It answers "where is the per-image second going" — network, mimetype sniff,
resize+upload, or throttle pacing — which is what tells you whether a bigger
instance would help (resize is CPU) or a higher rate would (pacing is not).

**Read the numbers as thread-seconds, not as a share of wall clock.** Image work
runs on ``ContextPool`` threads, so segments overlap each other and overlap the
serial parse. Totals therefore sum to more than the elapsed time, deliberately:
the ratio of accounted thread-seconds to wall time is the average concurrency
actually achieved, and is reported as such. An earlier version of this module
expressed each segment as a percentage of per-post wall time, which was only
meaningful while fetching was serial.

Segments, all measured as thread-seconds:

    sleep            RateLimiter.acquire — waiting for a paced slot
    download         HTTP GET of the source image
    process_file     mimetype sniff + metadata
    renditions       3 PIL resizes AND their 3 storage puts
    fetch_residual   the rest of one fetch attempt: the original's media.put,
                     plus retry backoff. media.put is a method on the app's
                     media instance rather than a module global, so it cannot be
                     wrapped like the others. Disambiguate with the failure
                     count: residual large with 0 failures is upload time, large
                     with failures is retry backoff.
    language         detect_language
    parse_post       whole-post parse on the generator thread (serial)

The three ``superdesk.media.renditions`` functions are module globals that
``update_renditions`` looks up at call time, so replacing the attribute
intercepts them. Everything else patched here is ours.
"""

import functools
import logging
import threading
import time

from superdesk.default_settings import strtobool

from pesacheck.ingest.util import env_int

logger = logging.getLogger(__name__)


def _enabled():
    import os

    raw = (os.environ.get("GHOST_INGEST_PROFILE") or "").strip()
    if not raw:
        return False
    try:
        return bool(strtobool(raw))
    except ValueError:
        logger.warning("Ignoring invalid GHOST_INGEST_PROFILE=%r", raw)
        return False


ENABLED = _enabled()

#: Log a running summary every N images, so a run killed by the soft time limit
#: still leaves numbers behind.
REPORT_EVERY = env_int("GHOST_INGEST_PROFILE_EVERY", 25)

#: Segments measured inside a single fetch attempt. fetch_residual is whatever
#: of that attempt these do not account for, so it is derived, not measured.
_FETCH_SEGMENTS = ("sleep", "download", "process_file", "renditions")
_SEGMENTS = _FETCH_SEGMENTS + ("fetch_residual", "language", "parse_post")

_lock = threading.Lock()
#: Per-thread segment totals. The residual is computed by subtracting this
#: thread's own segment time from its elapsed time; using the global totals
#: would subtract other threads' concurrent work and yield garbage.
_tls = threading.local()
_installed = False


def _thread_totals():
    totals = getattr(_tls, "totals", None)
    if totals is None:
        totals = _tls.totals = dict.fromkeys(_SEGMENTS, 0.0)
    return totals


class _Stats:
    def __init__(self):
        self.seconds = dict.fromkeys(_SEGMENTS, 0.0)
        self.calls = dict.fromkeys(_SEGMENTS, 0)
        self.posts = 0
        self.images_fetched = 0
        self.images_cached = 0
        self.failures = 0
        self.started = time.monotonic()

    def add(self, name, elapsed):
        _thread_totals()[name] += elapsed
        with _lock:
            self.seconds[name] += elapsed
            self.calls[name] += 1


_stats = _Stats()


def _timed(name):
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                _stats.add(name, time.monotonic() - start)

        return wrapper

    return decorate


def summary():
    s = _stats
    wall = time.monotonic() - s.started
    posts = s.posts or 1
    accounted = sum(s.seconds[n] for n in _SEGMENTS if n != "parse_post")
    images = s.images_fetched + s.images_cached
    lines = [
        "Ghost ingest profile:",
        f"  wall {wall:.1f}s  posts {s.posts}  images {images} "
        f"({s.images_fetched} fetched, {s.images_cached} cached, "
        f"{s.failures} failed)",
        f"  {images / posts:.2f} images/post, "
        f"{s.images_fetched / wall if wall else 0:.2f} fetches/s, "
        f"avg concurrency {accounted / wall if wall else 0:.1f}x",
        "  segment          thread-s   share      n      avg",
    ]
    for name in _SEGMENTS:
        total = s.seconds[name]
        # parse_post is serial wall time, not part of the concurrent pool work,
        # so it is excluded from the share denominator rather than diluting it.
        share = (
            (100.0 * total / accounted) if accounted and name != "parse_post" else 0.0
        )
        lines.append(
            f"  {name:14s} {total:9.1f}s {share:6.1f}%  "
            f"{s.calls[name]:6d}  {total / (s.calls[name] or 1):.3f}s"
        )
    return "\n".join(lines)


def log_summary():
    if ENABLED:
        logger.info("%s", summary())


def _patch(owner, attr, segment):
    """Wrap ``owner.attr`` with a timer, tolerating a rename.

    Deliberately non-fatal: ``install()`` runs from ``init_app``, so raising here
    takes down the whole app (and the celery worker) rather than just profiling.
    This module previously wrapped a parser method that was later renamed, which
    is exactly that failure.
    """
    func = getattr(owner, attr, None)
    if func is None:
        logger.warning(
            "Ghost profile: %s.%s is gone; %r will read as 0. "
            "Update ghost_profile.py.",
            getattr(owner, "__name__", owner),
            attr,
            segment,
        )
        return
    setattr(owner, attr, _timed(segment)(func))


def install():
    """Wrap the timed callables. Idempotent; a no-op unless ENABLED."""
    global _installed
    if not ENABLED or _installed:
        return
    _installed = True

    from superdesk.media import renditions as core_renditions

    from pesacheck.ingest import ghost_parser as parser_module
    from pesacheck.ingest.ghost_fetch_pool import RateLimiter
    from pesacheck.ingest.ghost_parser import GhostParser

    _patch(
        core_renditions, "download_file_from_feeding_service_or_directly", "download"
    )
    _patch(core_renditions, "process_file", "process_file")
    _patch(core_renditions, "generate_renditions", "renditions")

    # Pacing lives in the rate limiter now, not in the parser.
    _patch(RateLimiter, "acquire", "sleep")

    # detect_language is bound into the parser's namespace by ``from ... import``,
    # so the parser's reference is what has to be replaced.
    _patch(parser_module, "detect_language", "language")

    _wrap_fetch(GhostParser)
    _wrap_parse(GhostParser)

    logger.info(
        "Ghost ingest profiling ENABLED (summary every %d images). "
        "Segments are thread-seconds and overlap; see ghost_profile docstring.",
        REPORT_EVERY,
    )


def _wrap_fetch(parser_cls):
    """Count cache hits on the read path, and time real work on the fetch path."""
    inner_lookup = parser_cls._fetch_renditions_with_retry
    inner_fetch = parser_cls._fetch_payload

    @functools.wraps(inner_lookup)
    def counted_lookup(self, run, association, url):
        before = _stats.images_fetched
        result = inner_lookup(self, run, association, url)
        # A served-from-cache call is one that did not reach _fetch_payload.
        if _stats.images_fetched == before:
            with _lock:
                _stats.images_cached += 1
        return result

    @functools.wraps(inner_fetch)
    def timed_fetch(self, run, url):
        totals = _thread_totals()
        before = sum(totals[n] for n in _FETCH_SEGMENTS)
        start = time.monotonic()
        try:
            return inner_fetch(self, run, url)
        except Exception:
            with _lock:
                _stats.failures += 1
            raise
        finally:
            elapsed = time.monotonic() - start
            after = sum(_thread_totals()[n] for n in _FETCH_SEGMENTS)
            with _lock:
                _stats.images_fetched += 1
                _stats.seconds["fetch_residual"] += max(0.0, elapsed - (after - before))
                _stats.calls["fetch_residual"] += 1
                due = _stats.images_fetched % REPORT_EVERY == 0
            if due:
                log_summary()

    parser_cls._fetch_renditions_with_retry = counted_lookup
    parser_cls._fetch_payload = timed_fetch


def _wrap_parse(parser_cls):
    inner = parser_cls._parse_post

    @functools.wraps(inner)
    def timed_parse(self, *args, **kwargs):
        start = time.monotonic()
        try:
            return inner(self, *args, **kwargs)
        finally:
            _stats.add("parse_post", time.monotonic() - start)
            with _lock:
                _stats.posts += 1

    parser_cls._parse_post = timed_parse

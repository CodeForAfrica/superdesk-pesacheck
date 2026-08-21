"""Concurrency and stall-visibility helpers for Ghost image fetching.

Ghost ingest is dominated by per-image work: ~0.65s to pull an image from the
CDN plus ~0.4s to sniff, resize and upload it. Done serially that is ~27h for
PesaCheck's 92k distinct images, so the fetches run on a thread pool. The work
is blocking (core's ``update_renditions`` is synchronous), which is exactly what
threads are for — and Pillow releases the GIL during resizes, so they overlap.

Two things make that safe and debuggable, and they are the two classes here:

``RateLimiter``
    A pool of N threads would otherwise multiply the request rate by N. The
    parser's per-image ``min_interval`` is meaningless once several threads are
    in flight, so pacing moves here: threads reserve evenly spaced slots off one
    shared schedule, and ``1 / min_interval`` is the true aggregate rate no
    matter how many workers there are.

``InFlightTracker``
    Ghost ingest has a history of silent stalls, and a thread pool makes that
    worse: a hung worker shows up as nothing at all. So every fetch registers
    itself, a background thread logs what is outstanding on a fixed heartbeat,
    and if nothing completes for ``GHOST_IMAGE_STALL_SECONDS`` while work is
    in flight it logs the stuck URLs and how long each has been running.

``ContextPool`` wraps ThreadPoolExecutor so submitted work can reach the Quart
app context — ``superdesk.core.get_current_app()`` is ``quart.current_app``, a
contextvar-backed proxy, and a bare thread raises "Not within an app context".
"""

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pesacheck.ingest.util import env_float, env_int

logger = logging.getLogger(__name__)

# Threads fetching images. Profiling a 250-post run put 95% of all thread time
# in the download itself (5.3s average per image) against 3.8% in renditions and
# 0.3% in rate-limiting, so throughput here is latency x concurrency and nothing
# else — 8 workers averaged 0.88 images/s. Raise it for a real backfill: the host
# probe (scripts/ghost/probe_image_host.py) measured Medium serving 25.7 req/s at
# concurrency 32 with zero errors.
IMAGE_FETCH_WORKERS = env_int("GHOST_IMAGE_FETCH_WORKERS", 16)

# Posts whose images are prefetched together. Wants to be big enough to keep
# every worker busy: at ~14.6 images per post, 20 posts is ~290 images, and two
# windows are queued at once (see GhostParser._submit_window), so the pool has
# roughly 580 images of work available at any moment.
PREFETCH_WINDOW_POSTS = env_int("GHOST_PREFETCH_WINDOW_POSTS", 20)

# How often the heartbeat reports. 0 disables it.
HEARTBEAT_SECONDS = env_float("GHOST_INGEST_HEARTBEAT_SECONDS", 15.0)

# No completion for this long, with work in flight, is reported as a stall.
STALL_SECONDS = env_float("GHOST_IMAGE_STALL_SECONDS", 60.0)

# A single fetch taking longer than this is called out individually.
SLOW_FETCH_SECONDS = 15.0

# In-flight URLs listed per heartbeat line (oldest first).
HEARTBEAT_MAX_URLS = 5


class RateLimiter:
    """Evenly spaced slots, scheduled per host.

    ``acquire(host, 0.05)`` from 8 threads yields 20 requests/second to that
    host in total, not 20 per thread — the interval is an aggregate rate, so it
    means the same thing however many workers are running.

    Scheduling is *per host* rather than globally, which matters when a slow
    source and a fast one share the pool. On one shared schedule, reservations
    are FIFO: an image paced at 0.05s that reserves behind one paced at 3.0s
    waits the full 3.0s the slow reservation pushed out, so the slowest host
    ends up pacing every other. Keyed by host, each source gets its own spacing.

    Callers reserve under the lock and sleep outside it, so reserving never
    blocks another thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_allowed = {}

    def acquire(self, host, min_interval):
        """Block until this caller's slot for ``host`` arrives. Returns the wait."""
        if min_interval <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            slot = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = slot + min_interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def penalise(self, host, seconds):
        """Push ``host``'s schedule back, slowing every worker bound for it.

        Used after a fetch exhausts its retries. Sleeping in the failing worker
        instead would hold a pool slot for the whole cooldown while doing nothing
        — with N workers that is N times the intended penalty in lost capacity,
        and it lets one dead image stall a whole prefetch window. Advancing the
        schedule applies the backoff where it belongs: to the next requests,
        whoever makes them.
        """
        if seconds <= 0:
            return
        with self._lock:
            current = max(self._next_allowed.get(host, 0.0), time.monotonic())
            self._next_allowed[host] = current + seconds


class InFlightTracker:
    """Registry of running fetches, plus a heartbeat thread that reports them.

    The point is answering "what is it doing right now" for a run that appears
    hung, without attaching a debugger to a worker in ECS.
    """

    def __init__(self, label=""):
        self._lock = threading.Lock()
        self._active = {}
        self._token = 0
        self._stop = threading.Event()
        self._thread = None
        self.label = label
        self.context = ""
        self.completed = 0
        self.failed = 0
        self.cache_hits = 0
        self.slow = 0
        self.started_at = time.monotonic()
        self.last_completion = time.monotonic()
        self._stall_reported_at = 0.0

    # -- recording ---------------------------------------------------------

    def set_context(self, context):
        """Describe where ingest currently is, e.g. 'file.json post 12/100'."""
        with self._lock:
            self.context = context

    def start(self, url):
        with self._lock:
            self._token += 1
            token = self._token
            self._active[token] = (url, time.monotonic())
        return token

    def finish(self, token, ok=True):
        with self._lock:
            entry = self._active.pop(token, None)
            self.last_completion = time.monotonic()
            if ok:
                self.completed += 1
            else:
                self.failed += 1
        if entry:
            url, started = entry
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_FETCH_SECONDS:
                with self._lock:
                    self.slow += 1
                logger.warning("Ghost image fetch SLOW: %.1fs for %s", elapsed, url)
            return elapsed
        return 0.0

    def note_cache_hit(self):
        with self._lock:
            self.cache_hits += 1

    # -- reporting ---------------------------------------------------------

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            active = sorted(
                ((now - started, url) for url, started in self._active.values()),
                reverse=True,
            )
            return {
                "context": self.context,
                "active": len(self._active),
                "completed": self.completed,
                "failed": self.failed,
                "cache_hits": self.cache_hits,
                "slow": self.slow,
                "since_completion": now - self.last_completion,
                "elapsed": now - self.started_at,
                "oldest": active[:HEARTBEAT_MAX_URLS],
            }

    def summary(self):
        s = self.snapshot()
        done = s["completed"] + s["cache_hits"]
        rate = s["completed"] / s["elapsed"] if s["elapsed"] else 0.0
        return (
            f"{self.label}: {done} images ({s['completed']} fetched, "
            f"{s['cache_hits']} cached, {s['failed']} failed, {s['slow']} slow) "
            f"in {s['elapsed']:.1f}s = {rate:.2f} fetch/s"
        )

    def _report(self):
        s = self.snapshot()
        logger.info(
            "Ghost ingest heartbeat [%s]: active=%d fetched=%d cached=%d "
            "failed=%d slow=%d last_completion=%.1fs ago elapsed=%.1fs",
            s["context"] or self.label,
            s["active"],
            s["completed"],
            s["cache_hits"],
            s["failed"],
            s["slow"],
            s["since_completion"],
            s["elapsed"],
        )
        for elapsed, url in s["oldest"]:
            logger.info("    in flight %6.1fs  %s", elapsed, url)

        # Nothing finishing while work is outstanding is the signature of the
        # stalls this pipeline has hit before. Say so loudly, once per episode,
        # and name the URLs so the cause is identifiable from logs alone.
        if s["active"] and s["since_completion"] >= STALL_SECONDS:
            if self.last_completion > self._stall_reported_at:
                self._stall_reported_at = self.last_completion
                logger.error(
                    "Ghost ingest STALLED: no image completed for %.1fs with %d "
                    "in flight at [%s]. Stuck URLs above. Likely a fetch hung "
                    "past its socket timeout, or the source host stopped "
                    "responding without closing the connection.",
                    s["since_completion"],
                    s["active"],
                    s["context"] or self.label,
                )

    def _loop(self):
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self._report()
            except Exception:
                logger.warning("Ghost heartbeat failed", exc_info=True)

    def start_heartbeat(self):
        if HEARTBEAT_SECONDS <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="GhostIngestHeartbeat", daemon=True
        )
        self._thread.start()
        logger.info(
            "Ghost ingest heartbeat every %.1fs (stall threshold %.1fs)",
            HEARTBEAT_SECONDS,
            STALL_SECONDS,
        )

    def stop_heartbeat(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=HEARTBEAT_SECONDS + 5)
        self._thread = None


class ContextPool:
    """Thread pool whose jobs can reach the Quart app context.

    ``get_current_app()`` resolves ``quart.current_app``, a contextvar proxy, so
    a plain worker thread raises "Not within an app context". Each job therefore
    runs inside its own ``contextvars.copy_context()``. It must be a *fresh copy
    per job*: a Context cannot be entered twice concurrently, so sharing one
    across workers raises "cannot enter context: already entered". The copy is
    taken in the submitting thread, which is the one holding the context.
    """

    def __init__(self, workers=None, name="GhostImageFetch"):
        self.workers = workers or IMAGE_FETCH_WORKERS
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix=name
        )

    def submit(self, fn, *args, **kwargs):
        ctx = contextvars.copy_context()
        return self._pool.submit(ctx.run, lambda: fn(*args, **kwargs))

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait)

import hashlib
import json
import logging
import os
import random
import time
from copy import deepcopy
from datetime import datetime, timezone

from superdesk.errors import ParserError
from superdesk.etree import parse_html
from superdesk.io.feed_parsers import FileFeedParser
from superdesk.io.registry import register_feed_parser
from superdesk.media.renditions import update_renditions
from superdesk.metadata.item import (
    CONTENT_TYPE,
    FORMAT,
    FORMATS,
    GUID_FIELD,
    GUID_TAG,
    ITEM_TYPE,
)
from superdesk.metadata.utils import generate_guid
from superdesk.text_utils import get_text
from superdesk.utc import utcnow

from pesacheck.debunk import debunk_rating
from pesacheck.ghost_urls import (
    GHOST_URL_PLACEHOLDER,
    build_fetch_headers,
    fetch_host,
    is_ingestable_post,
    is_medium_cdn_url,
    resolve_url,
)
from pesacheck.ingest.ghost_fetch_pool import (
    PREFETCH_WINDOW_POSTS,
    ContextPool,
    InFlightTracker,
    RateLimiter,
)
from pesacheck.ingest.util import env_float, env_int
from pesacheck.language import detect_language, normalise_language_code

logger = logging.getLogger(__name__)

# Ghost exports replace the site URL with this portable placeholder in
# feature_image, inline <img> src, and body links. It must be substituted with
# the real site URL (provider config ``url``) before any image is fetched:
# left as-is it has no http scheme, so core's download path treats it as a
# relative URL and calls url_for(_external=True), which raises
# "Unable to create a url adapter" in the Celery worker (no request context).

# bytes to peek at for fast can_parse check
_PEEK_SIZE = 512

# Image-fetch throttle. These pace/space downloads to keep the Ghost site and
# CDNs from 403-ing or rate-limiting a bulk import — see _build_fetch_headers
# and _fetch_renditions_with_retry. Every value is overridable via env so a
# one-off backfill from an origin you control can turn the spacing way down
# (e.g. GHOST_IMAGE_FETCH_MIN_INTERVAL=0.1) without a code change, then be
# restored to polite defaults for steady-state ingest.
_IMAGE_FETCH_RETRIES = env_int("GHOST_IMAGE_FETCH_RETRIES", 4)
_IMAGE_FETCH_TIMEOUT = env_float("GHOST_IMAGE_FETCH_TIMEOUT", 20)
_IMAGE_FETCH_BASE_BACKOFF_SECONDS = env_float("GHOST_IMAGE_FETCH_BASE_BACKOFF", 0.75)
_IMAGE_FETCH_JITTER_SECONDS = env_float("GHOST_IMAGE_FETCH_JITTER", 0.25)
_IMAGE_FETCH_SUCCESS_THROTTLE_SECONDS = env_float(
    "GHOST_IMAGE_FETCH_SUCCESS_THROTTLE", 0.0
)
_IMAGE_FETCH_MIN_INTERVAL_SECONDS = env_float("GHOST_IMAGE_FETCH_MIN_INTERVAL", 0.05)
# Cooldowns default to off. They existed to pace a single-threaded fetcher after
# a failure; RateLimiter now paces every request from every worker, and per-image
# retry backoff still handles the individual failure. Set these if a host needs
# adaptive backoff on top — note they now delay ALL workers, not just one.
_IMAGE_FETCH_FAILURE_COOLDOWN_SECONDS = env_float(
    "GHOST_IMAGE_FETCH_FAILURE_COOLDOWN", 0.0
)

# Images repeat heavily across a corpus (PesaCheck: ~18% of refs are duplicates,
# mostly avatars and logos), so rendition metadata is cached across files for the
# life of the run rather than reset per file. Capped because a payload is ~3.5KB
# and a 92k-image corpus would hold ~320MB resident in the worker; past the cap
# duplicates are simply re-fetched. The cap cannot cause an image to be fetched
# twice within one window — see GhostRun.payloads.
_IMAGE_CACHE_MAX_ENTRIES = env_int("GHOST_IMAGE_CACHE_MAX", 50000)

# Medium hosts ~91% of PesaCheck's images, not a stray tail, so its policy governs
# the whole run. min_interval is now an AGGREGATE rate (RateLimiter schedules per
# host across all workers), so the old 3.0s cap meant 0.33 req/s total no matter
# how many workers ran — 84k distinct Medium URLs x 3.0s is ~70h, i.e. the thread
# pool bought nothing. 0.05s = 20 req/s, inside the rate measured clean (zero
# errors to 32-way, 25.7 req/s) by scripts/ghost/probe_image_host.py. Re-probe
# before raising it; the ladder never found the host's actual ceiling.
_MEDIUM_FETCH_RETRIES = env_int("GHOST_MEDIUM_FETCH_RETRIES", 4)
_MEDIUM_FETCH_BASE_BACKOFF_SECONDS = env_float("GHOST_MEDIUM_FETCH_BASE_BACKOFF", 0.75)
_MEDIUM_FETCH_MIN_INTERVAL_SECONDS = env_float("GHOST_MEDIUM_FETCH_MIN_INTERVAL", 0.05)
_MEDIUM_FETCH_FAILURE_COOLDOWN_SECONDS = env_float(
    "GHOST_MEDIUM_FETCH_FAILURE_COOLDOWN", 0.0
)


class GhostRun:
    """State for one ``iter_items`` call.

    ``GhostParser`` is registered as a module-level singleton
    (``register_feed_parser(GhostParser.NAME, GhostParser())``), so anything
    per-run stored on it is shared by every call. Two overlapping runs — a second
    ghost provider, or ``parse()`` entered while another generator is live —
    would clobber each other's pool and tracker, and whichever finished first
    would ``shutdown()`` the pool the other was still submitting to. Keeping this
    in a run object handed down the call chain makes that structurally impossible.

    ``payloads`` holds this window's fetched renditions. It exists so the
    prefetch never depends on the shared cache accepting an entry: once the cache
    hits its cap, ``_cache_put`` stops storing, and without this the serial pass
    would miss and re-fetch — every prefetched image downloaded, resized and
    uploaded twice, the second time serially on the generator thread.

    ``warm`` holds renditions recovered from the database for images this corpus
    has already ingested, keyed by URL (see GhostFeedingService._warm_image_cache).
    Unlike ``payloads`` it spans the whole file, and unlike the parser's in-process
    cache it survives a worker restart and a different prefork child — which is
    what makes resuming a part-consumed file cheap instead of a full re-fetch.
    """

    __slots__ = ("basename", "ghost_url", "payloads", "pool", "tracker", "warm")

    def __init__(self, basename, ghost_url, pool, tracker, warm=None):
        self.basename = basename
        self.ghost_url = ghost_url
        self.pool = pool
        self.tracker = tracker
        self.payloads = {}
        self.warm = warm or {}


class GhostParser(FileFeedParser):
    """
    Feed Parser for parsing Ghost CMS JSON export files.

    Expects the standard Ghost export format (db[0].data.posts etc.).
    Only published posts of type 'post' are imported.
    """

    NAME = "ghost"
    label = "Ghost CMS Parser"

    def can_parse(self, file_path):
        try:
            with open(file_path, "rb") as f:
                head = f.read(_PEEK_SIZE).decode("utf-8", errors="ignore").strip()
            return head.startswith("{") and '"db"' in head
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Image helpers — same pattern as MediumParser
    # ------------------------------------------------------------------

    def _get_fetch_policy(self, url):
        if is_medium_cdn_url(url):
            return {
                "retries": _MEDIUM_FETCH_RETRIES,
                "base_backoff": _MEDIUM_FETCH_BASE_BACKOFF_SECONDS,
                "min_interval": _MEDIUM_FETCH_MIN_INTERVAL_SECONDS,
                "failure_cooldown": _MEDIUM_FETCH_FAILURE_COOLDOWN_SECONDS,
            }

        return {
            "retries": _IMAGE_FETCH_RETRIES,
            "base_backoff": _IMAGE_FETCH_BASE_BACKOFF_SECONDS,
            "min_interval": _IMAGE_FETCH_MIN_INTERVAL_SECONDS,
            "failure_cooldown": _IMAGE_FETCH_FAILURE_COOLDOWN_SECONDS,
        }

    # ------------------------------------------------------------------
    # Shared state. The parser is registered as a module-level singleton, and
    # image fetches now run on a thread pool, so everything mutable here is
    # guarded. Set up in __init__ rather than in iter_items so the cache and the
    # rate limiter outlive a single file.
    # ------------------------------------------------------------------

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Shared across runs on purpose: rendition metadata for a URL is
        # immutable (the guid is a sha1 of the URL), so a hit is always valid.
        # Plain dict ops are atomic under the GIL and a lost race only costs a
        # duplicate fetch, so this needs no lock.
        self._image_cache = {}
        self._cache_capped = False
        self._rate_limiter = RateLimiter()

    def _cache_lookup(self, url):
        """Cached payload, a cached Exception, or None if unseen.

        One dict for both outcomes: a URL that already exhausted its retry
        ladder must not be retried inline, or every dead image costs its ladder
        twice.
        """
        return self._image_cache.get(url)

    def _cache_store(self, url, payload_or_error):
        if len(self._image_cache) >= _IMAGE_CACHE_MAX_ENTRIES:
            if not self._cache_capped:
                self._cache_capped = True
                logger.warning(
                    "Ghost image cache full at %d entries; cross-file duplicates "
                    "will be re-fetched. Raise GHOST_IMAGE_CACHE_MAX to avoid.",
                    _IMAGE_CACHE_MAX_ENTRIES,
                )
            return
        self._image_cache[url] = payload_or_error

    def _generate_image_guid(self, url):
        guid_hash = hashlib.sha1(url.encode("utf8")).hexdigest()
        return generate_guid(type=GUID_TAG, id=guid_hash + "-image")

    def _fetch_renditions_with_retry(self, run, association, url):
        """Attach renditions for ``url`` to ``association``.

        Served from this window's payloads first, then the cross-run cache;
        after ``_prefetch_window`` has run that is the common path. A miss falls
        through to fetching inline, so correctness never depends on the prefetch
        having succeeded.
        """
        payload = run.payloads.get(url) or run.warm.get(url)
        if payload is None:
            payload = self._cache_lookup(url)

        if isinstance(payload, Exception):
            # Already exhausted its retry ladder during the prefetch.
            raise payload
        if payload is not None:
            association.update(deepcopy(payload))
            if run.tracker:
                run.tracker.note_cache_hit()
            return

        association.update(deepcopy(self._fetch_payload(run, url)))

    def _fetch_payload(self, run, url):
        """Download and render ``url``, record the result, and return it.

        Thread-safe: ``holder`` is a local, core's ``update_renditions`` only
        writes into the dict it is handed, and the caches are plain dicts whose
        item assignment is atomic. Raises the last error if every attempt fails.
        """
        policy = self._get_fetch_policy(url)
        host = fetch_host(url)
        last_error = None

        for attempt in range(1, policy["retries"] + 1):
            waited = self._rate_limiter.acquire(host, policy["min_interval"])
            token = run.tracker.start(url) if run.tracker else None
            started = time.monotonic()
            holder = {}
            try:
                update_renditions(
                    holder,
                    url,
                    None,
                    request_kwargs={
                        "timeout": _IMAGE_FETCH_TIMEOUT,
                        "headers": build_fetch_headers(url),
                    },
                )
                # No deepcopy: holder is a fresh local that nothing else reads,
                # and callers deepcopy on the way out.
                payload = {
                    "renditions": holder.get("renditions"),
                    "mimetype": holder.get("mimetype"),
                    "filemeta": holder.get("filemeta"),
                    "filemeta_json": holder.get("filemeta_json"),
                }
                run.payloads[url] = payload
                self._cache_store(url, payload)
                if run.tracker:
                    run.tracker.finish(token, ok=True)
                logger.debug(
                    "Ghost image ok in %.2fs (waited %.2fs, attempt %d): %s",
                    time.monotonic() - started,
                    waited,
                    attempt,
                    url,
                )
                if _IMAGE_FETCH_SUCCESS_THROTTLE_SECONDS > 0:
                    time.sleep(_IMAGE_FETCH_SUCCESS_THROTTLE_SECONDS)
                return payload
            except Exception as ex:
                if run.tracker:
                    run.tracker.finish(token, ok=False)
                last_error = ex
                if attempt == policy["retries"]:
                    break

                delay = (policy["base_backoff"] * attempt) + random.uniform(
                    0, _IMAGE_FETCH_JITTER_SECONDS
                )
                logger.warning(
                    "Image fetch failed for %s after %.2fs (attempt %s/%s), "
                    "retrying in %.2fs: %s",
                    url,
                    time.monotonic() - started,
                    attempt,
                    policy["retries"],
                    delay,
                    ex,
                )
                time.sleep(delay)

        if policy["failure_cooldown"] > 0:
            # Back off after exhausting retries, to avoid a retry storm against a
            # host that has started failing. Applied to that host's schedule
            # rather than slept here: parking this worker would hold a pool slot
            # doing nothing and let one dead image stall the whole window.
            self._rate_limiter.penalise(
                host,
                policy["failure_cooldown"]
                + random.uniform(0, _IMAGE_FETCH_JITTER_SECONDS),
            )

        logger.error(
            "Ghost image FAILED after %d attempts: %s (%s)",
            policy["retries"],
            url,
            last_error,
        )
        self._cache_store(url, last_error)
        raise last_error

    def _add_image(self, run, item, url, alt_text="", description_text=""):
        """Attach ``url`` as an association and return its local storage href."""
        associations = item.setdefault("associations", {})
        association = {
            ITEM_TYPE: CONTENT_TYPE.PICTURE,
            GUID_FIELD: self._generate_image_guid(url),
            # Version metadata is what lets core's is_new_version() recognise an
            # already-ingested image as unchanged. Without it, is_new_version
            # falls back to a field-by-field content comparison that never
            # matches (freshly-generated renditions differ byte-for-byte from
            # the stored ones), so on EVERY ingest cycle core re-transfers —
            # re-downloads and re-renders — every rendition of every image. That
            # rendition churn consumes the whole update_provider soft-time-limit
            # before auto-publish is reached, so on a small (2-vCPU) worker the
            # run is killed and nothing ever publishes. The guid is stable
            # (sha1 of the source URL), so a constant version is correct: same
            # URL == same immutable image. versioncreated is the post's own
            # version time — for images already stored under the old, version-less
            # path it is older than their stored (ingest-time) versioncreated, so
            # is_new_version short-circuits to False without a transitional
            # re-transfer. These fields are also what a normal ingested picture
            # item carries, so this is strictly more correct.
            "version": 1,
            "versioncreated": item.get("versioncreated"),
            "firstcreated": item.get("firstcreated"),
            "headline": item.get("headline", ""),
            "alt_text": alt_text,
            "description_text": description_text,
        }
        self._fetch_renditions_with_retry(run, association, url)

        if "featuremedia" not in associations:
            key = "featuremedia"
        else:
            key = "embedded" + str(len(associations) - 1)

        associations[key] = association

        return association.get("renditions", {}).get("original", {}).get("href")

    def _iter_image_refs(self, run, post):
        """Yield ``(url, alt_text, description_text)`` for every image in ``post``.

        Single definition of "which images does this post contribute", driven by
        both the prefetch and the parse. They used to extract this separately,
        and the two could not diverge loudly: a mismatch would leave the prefetch
        warming the wrong URLs, which shows up only as a slow run.

        Document order, feature image first, matching the association naming in
        ``_add_image`` (featuremedia, then embedded0..n).
        """
        feature = resolve_url(post.get("feature_image"), run.ghost_url)
        if feature:
            yield feature, "", ""

        html = self._post_html(run, post)
        if not html:
            return
        try:
            root = parse_html(html, "html")
        except Exception as ex:
            logger.warning("Failed to parse HTML for inline images: %s", ex)
            return

        for img in root.xpath(".//img"):
            url = resolve_url(img.get("src"), run.ghost_url)
            if not url:
                logger.debug("Skipping image with relative URL %r", img.get("src"))
                continue

            description_text = ""
            parent = img.getparent()
            if parent is not None and parent.tag == "figure":
                figcaption = parent.find(".//figcaption")
                if figcaption is not None:
                    description_text = (
                        figcaption.text_content()
                        if hasattr(figcaption, "text_content")
                        else (figcaption.text or "")
                    ).strip()

            yield url, img.get("alt") or "", description_text

    def _post_html(self, run, post):
        html = post.get("html") or ""
        if run.ghost_url and html:
            html = html.replace(GHOST_URL_PLACEHOLDER, run.ghost_url)
        return html

    def _parse_images(self, run, item, post):
        """Attach every image in ``post`` and rewrite the body to point at them."""
        url_rewrites = {}
        for url, alt_text, description_text in self._iter_image_refs(run, post):
            try:
                local_href = self._add_image(run, item, url, alt_text, description_text)
                if local_href:
                    url_rewrites[url] = local_href
            except Exception as ex:
                logger.warning("Failed to fetch Ghost image %s: %s", url, ex)

        if url_rewrites:
            body = item.get("body_html") or ""
            for external_url, local_href in url_rewrites.items():
                body = body.replace(external_url, local_href)
            item["body_html"] = body

    def _parse_language(self, post, tags, text):
        """Resolve the item language from ``locale``, then tags, then body text.

        Ghost's own ``locale`` is authoritative when set, but PesaCheck's export
        leaves it null on every post, so in practice the language comes from the
        post's language tag. Posts predating that tagging convention fall
        through to text classification.
        """
        locale = normalise_language_code(post.get("locale"))
        if locale:
            return locale

        # Offer both the display name and the slug: either may be the form that
        # names the language ("Afaan Oromo" / "afaan-oromo").
        tag_labels = [
            (tag["sort_order"], label)
            for tag in tags
            for label in (tag["name"], tag["slug"])
        ]

        return detect_language(tag_labels, text)

    def _parse_date(self, value):
        if not value:
            return utcnow()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
        raise ValueError("Unrecognised date format: %r" % value)

    # ------------------------------------------------------------------
    # Single post → Superdesk item
    # ------------------------------------------------------------------

    def _parse_post(self, run, post, authors_by_post, tags_by_post):
        post_id = post.get("id", "")

        authors = sorted(
            authors_by_post.get(post_id, []), key=lambda x: x["sort_order"]
        )
        byline = ", ".join(a["name"] for a in authors if a.get("name"))

        tags = sorted(tags_by_post.get(post_id, []), key=lambda x: x["sort_order"])
        keywords = [t["name"] for t in tags if t.get("name")]

        firstcreated = self._parse_date(post.get("created_at"))
        published_at = post.get("published_at") or post.get("updated_at")
        versioncreated = self._parse_date(published_at)
        firstpublished = (
            self._parse_date(post.get("published_at"))
            if post.get("published_at")
            else None
        )

        html = self._post_html(run, post)

        item = {
            ITEM_TYPE: CONTENT_TYPE.TEXT,
            GUID_FIELD: post.get("uuid") or generate_guid(type=GUID_TAG),
            FORMAT: FORMATS.HTML,
            "headline": post.get("title") or "",
            "abstract": post.get("custom_excerpt") or "",
            "slugline": post.get("slug") or "",
            "byline": byline,
            "keywords": keywords,
            "body_html": html,
            "source": "Ghost",
            "firstcreated": firstcreated,
            "versioncreated": versioncreated,
            "firstpublished": firstpublished,
        }

        # Ghost exports a markup-free rendering of the body; prefer it for
        # language detection so HTML tag names don't dilute the word counts.
        # Older exports omit it, so strip the markup ourselves in that case.
        body_text = post.get("plaintext") or get_text(html, content="html")
        sample = " ".join(part for part in (post.get("title") or "", body_text) if part)
        item["language"] = self._parse_language(post, tags, sample)

        # The verdict is carried in the headline prefix; record it as the Debunk
        # rating. Unknown or absent prefixes leave the item without a rating.
        rating = debunk_rating(item["headline"])
        if rating:
            item.setdefault("subject", []).append(rating)

        self._parse_images(run, item, post)

        return item

    def image_guid(self, url):
        """Public alias for the stable per-URL image guid.

        The feeding service needs the same rule to look these images up in the
        database, and the two must not drift — a mismatch would silently disable
        the warm cache and re-fetch everything.

        Note the guid embeds the current year (``tag:{domain}:{year}:{sha1}-image``
        via ``generate_guid``), so a run spanning 31 December stops matching its
        own earlier images. Harmless for a single backfill; worth knowing.
        """
        return self._generate_image_guid(url)

    def iter_image_urls(self, file_path, provider=None):
        """Yield every fetchable image URL in a file, without fetching anything.

        A deliberately cheap pass (~1s on a 5MB export) so the feeding service can
        ask the database which of these it already holds before the expensive pass
        starts. Uses the same _iter_image_refs as the real parse, so coverage
        cannot drift.
        """
        ghost_url = ((provider or {}).get("config", {}).get("url") or "").rstrip("/")
        run = GhostRun(os.path.basename(file_path), ghost_url, None, None)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            posts = data["db"][0]["data"].get("posts", [])
        except Exception as ex:
            logger.warning("Could not scan %s for image URLs: %s", file_path, ex)
            return

        seen = set()
        for post in posts:
            if not is_ingestable_post(post):
                continue
            for url, _alt, _desc in self._iter_image_refs(run, post):
                if url not in seen:
                    seen.add(url)
                    yield url

    # ------------------------------------------------------------------
    # Prefetch
    # ------------------------------------------------------------------

    def _prefetch_window(self, run, posts, label):
        """Fetch every not-yet-known image in ``posts`` concurrently.

        Purely an optimisation: results land in ``run.payloads`` and the serial
        pass reads them from there. Failures are recorded, never raised — the
        serial pass surfaces them per image with its own error handling.
        """
        run.payloads = {}
        wanted, seen = [], set()
        for post in posts:
            for url, _alt, _desc in self._iter_image_refs(run, post):
                if url in seen:
                    continue
                seen.add(url)
                if url not in run.warm and self._cache_lookup(url) is None:
                    wanted.append(url)

        logger.info(
            "Ghost prefetch %s: %d posts, %d images to fetch (%d already known) "
            "on %d workers",
            label,
            len(posts),
            len(wanted),
            len(seen) - len(wanted),
            run.pool.workers,
        )
        if not wanted:
            return

        started = time.monotonic()
        results = run.pool.map(lambda url: self._prefetch_one(run, url), wanted)
        done = sum(1 for ok in results if ok)
        elapsed = time.monotonic() - started
        logger.info(
            "Ghost prefetch %s done: %d ok, %d failed in %.1fs (%.2f images/s)",
            label,
            done,
            len(results) - done,
            elapsed,
            done / elapsed if elapsed else 0.0,
        )

    def _prefetch_one(self, run, url):
        try:
            self._fetch_payload(run, url)
            return True
        except Exception:
            # Already logged with the URL and cause by _fetch_payload.
            return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def iter_items(self, file_path, provider=None, warm_cache=None):
        """Parse a Ghost JSON export file and yield Superdesk items one at a time.

        Images are fetched a window of posts at a time on a thread pool before
        those posts are parsed, so the ~1s of network + resize + upload per image
        overlaps instead of accumulating. The parse itself stays serial and in
        document order — only fetching is concurrent — so items are yielded
        exactly as before.
        """
        ghost_url = ((provider or {}).get("config", {}).get("url") or "").rstrip("/")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            db_data = data["db"][0]["data"]
            posts = db_data.get("posts", [])
            users = db_data.get("users", [])
            tags = db_data.get("tags", [])
            posts_authors = db_data.get("posts_authors", [])
            posts_tags = db_data.get("posts_tags", [])

        except Exception as ex:
            raise ParserError.parseFileError(file_path, ex)

        users_by_id = {u["id"]: u for u in users}
        tags_by_id = {t["id"]: t for t in tags}

        authors_by_post = {}
        for pa in posts_authors:
            pid = pa.get("post_id")
            user = users_by_id.get(pa.get("author_id"))
            if pid and user:
                authors_by_post.setdefault(pid, []).append(
                    {
                        "name": user.get("name", ""),
                        "sort_order": pa.get("sort_order", 0),
                    }
                )

        tags_by_post = {}
        for pt in posts_tags:
            pid = pt.get("post_id")
            tag = tags_by_id.get(pt.get("tag_id"))
            if pid and tag:
                tags_by_post.setdefault(pid, []).append(
                    {
                        "name": tag.get("name", ""),
                        "slug": tag.get("slug", ""),
                        # Coerce a null sort_order so the sort below can't blow
                        # up comparing None to an int.
                        "sort_order": pt.get("sort_order") or 0,
                    }
                )

        candidates = [post for post in posts if is_ingestable_post(post)]
        basename = os.path.basename(file_path)
        size = max(1, PREFETCH_WINDOW_POSTS)
        windows = [candidates[i : i + size] for i in range(0, len(candidates), size)]

        logger.info(
            "Ghost parse start: %s — %d posts in export, %d to ingest, "
            "%d windows of %d, %d images pre-warmed from the database",
            basename,
            len(posts),
            len(candidates),
            len(windows),
            size,
            len(warm_cache or {}),
        )

        tracker = InFlightTracker(label=f"ghost:{basename}")
        run = GhostRun(basename, ghost_url, ContextPool(), tracker, warm=warm_cache)
        tracker.start_heartbeat()
        file_started = time.monotonic()
        position = parsed = 0

        try:
            for window_no, chunk in enumerate(windows, 1):
                label = f"{basename} window {window_no}/{len(windows)}"
                tracker.set_context(label)
                self._prefetch_window(run, chunk, label)

                for post in chunk:
                    position += 1
                    tracker.set_context(
                        f"{basename} post {position}/{len(candidates)} "
                        f"(id={post.get('id')})"
                    )
                    try:
                        item = self._parse_post(
                            run, post, authors_by_post, tags_by_post
                        )
                    except Exception as ex:
                        logger.warning(
                            "Failed to parse Ghost post %s: %s", post.get("id"), ex
                        )
                        continue
                    parsed += 1
                    yield item
        finally:
            # Runs on normal exhaustion, on an exception, and on the consumer
            # abandoning the generator (GeneratorExit) — the pool must not be
            # left with live threads in any of those cases.
            tracker.set_context(f"{basename} shutting down")
            run.pool.shutdown(wait=True)
            tracker.stop_heartbeat()
            run.payloads = {}
            logger.info(
                "Ghost parse done: %s — %d/%d posts yielded in %.1fs. %s. "
                "Image cache holds %d entries%s",
                basename,
                parsed,
                len(candidates),
                time.monotonic() - file_started,
                tracker.summary(),
                len(self._image_cache),
                " (capped)" if self._cache_capped else "",
            )

    async def parse(self, file_path, provider=None):
        """Parse a Ghost JSON export file and return a list of Superdesk items.

        ``FeedParser.parse`` is a coroutine in the async core, so this override
        is ``async``. The parsing itself is synchronous (``iter_items``), so we
        simply materialise the generator here.
        """
        return list(self.iter_items(file_path, provider))


register_feed_parser(GhostParser.NAME, GhostParser())

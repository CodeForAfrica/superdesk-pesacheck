import hashlib
import json
import logging
import random
import time
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlparse

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
from pesacheck.ingest.util import env_float, env_int
from pesacheck.language import detect_language, normalise_language_code

logger = logging.getLogger(__name__)

# Ghost exports replace the site URL with this portable placeholder in
# feature_image, inline <img> src, and body links. It must be substituted with
# the real site URL (provider config ``url``) before any image is fetched:
# left as-is it has no http scheme, so core's download path treats it as a
# relative URL and calls url_for(_external=True), which raises
# "Unable to create a url adapter" in the Celery worker (no request context).
GHOST_URL_PLACEHOLDER = "__GHOST_URL__"

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
_IMAGE_FETCH_MIN_INTERVAL_SECONDS = env_float("GHOST_IMAGE_FETCH_MIN_INTERVAL", 0.1)
_IMAGE_FETCH_FAILURE_COOLDOWN_SECONDS = env_float(
    "GHOST_IMAGE_FETCH_FAILURE_COOLDOWN", 3.0
)

# Medium CDN is the problematic source in current imports, so use a slower policy there.
_MEDIUM_FETCH_RETRIES = env_int("GHOST_MEDIUM_FETCH_RETRIES", 10)
_MEDIUM_FETCH_BASE_BACKOFF_SECONDS = env_float("GHOST_MEDIUM_FETCH_BASE_BACKOFF", 2.5)
_MEDIUM_FETCH_MIN_INTERVAL_SECONDS = env_float("GHOST_MEDIUM_FETCH_MIN_INTERVAL", 3.0)
_MEDIUM_FETCH_FAILURE_COOLDOWN_SECONDS = env_float(
    "GHOST_MEDIUM_FETCH_FAILURE_COOLDOWN", 8.0
)

_IMAGE_FETCH_HEADERS = {
    # Some CDNs are strict about clients and may reject the default python
    # user-agent. The Referer is deliberately NOT hardcoded here: it must match
    # each image's OWN origin and is set per-request in _build_fetch_headers.
    # A hardcoded cross-site Referer (this used to be "https://medium.com/",
    # a leftover from when images were Medium-hosted) makes PesaCheck's own
    # Ghost site 403 every image — see _build_fetch_headers for the full story.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) GhostIngest/1.0",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


class GhostParser(FileFeedParser):
    """
    Feed Parser for parsing Ghost CMS JSON export files.

    Expects the standard Ghost export format (db[0].data.posts etc.).
    Only published posts of type 'post' are imported.
    """

    NAME = "ghost"
    label = "Ghost CMS Parser"

    def __init__(self):
        super().__init__()
        self._last_image_fetch_ts = 0.0
        self._image_assoc_cache = {}
        # Real Ghost site URL, taken from the provider config in iter_items and
        # substituted for GHOST_URL_PLACEHOLDER in image/body URLs.
        self._ghost_url = ""

    def _resolve_url(self, url):
        """Turn a Ghost export URL into a fetchable absolute URL.

        Substitutes the ``__GHOST_URL__`` placeholder with the configured site
        URL and returns the result only if it is now an absolute http(s) URL.
        Anything still relative (no configured site URL, or a genuinely relative
        link) is skipped rather than handed to the downloader, which would crash
        in the worker trying to resolve it against the app host.
        """
        if not url:
            return None
        if self._ghost_url:
            url = url.replace(GHOST_URL_PLACEHOLDER, self._ghost_url)
        if GHOST_URL_PLACEHOLDER in url or not urlparse(url).scheme:
            logger.warning(
                "Skipping image with unresolved/relative URL %r "
                "(set the provider config 'url' to the Ghost site to fetch these)",
                url,
            )
            return None
        return url

    def _is_medium_cdn_url(self, url):
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return "medium.com" in host

    def _get_fetch_policy(self, url):
        if self._is_medium_cdn_url(url):
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

    def _sleep_before_next_fetch(self, min_interval):
        elapsed = time.monotonic() - self._last_image_fetch_ts
        wait_for = min_interval - elapsed
        if wait_for > 0:
            # Pace all fetches (not just retries) to avoid hammering remote CDN endpoints.
            time.sleep(wait_for + random.uniform(0, _IMAGE_FETCH_JITTER_SECONDS))

    def _mark_fetch_done(self):
        self._last_image_fetch_ts = time.monotonic()

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

    def _generate_image_guid(self, url):
        guid_hash = hashlib.sha1(url.encode("utf8")).hexdigest()
        return generate_guid(type=GUID_TAG, id=guid_hash + "-image")

    def _build_fetch_headers(self, url):
        """Return image-fetch headers with a same-origin Referer.

        The Referer must match the image's OWN origin. PesaCheck's Ghost site
        (pesacheck.org) sits behind Cloudflare with a hotlink rule that returns
        403 to any request carrying a cross-site Referer. The parser used to
        send a fixed ``Referer: https://medium.com/`` (from when images were
        Medium-hosted), so EVERY pesacheck.org image 403'd; each failure then
        burned its full retry ladder (~12s), and a ~245-image export could never
        finish parsing within the ``update_provider`` soft-time-limit (1800s).
        The file was therefore never moved and re-failed every ingest cycle —
        nothing past the first dozen posts was ever ingested or published.

        Sending a Referer equal to the image's own scheme+host satisfies the
        hotlink rule for any source (pesacheck.org and the few remaining Medium
        CDN images alike). A relative/hostless URL never reaches here (see
        _resolve_url), but guard anyway and omit the Referer if we can't build one.
        """
        headers = dict(_IMAGE_FETCH_HEADERS)
        parsed = urlparse(url)
        if parsed.scheme and parsed.hostname:
            headers["Referer"] = f"{parsed.scheme}://{parsed.hostname}/"
        return headers

    def _fetch_renditions_with_retry(self, association, url):
        if url in self._image_assoc_cache:
            cached = self._image_assoc_cache[url]
            association.update(deepcopy(cached))
            return

        policy = self._get_fetch_policy(url)
        last_error = None
        for attempt in range(1, policy["retries"] + 1):
            try:
                self._sleep_before_next_fetch(policy["min_interval"])
                update_renditions(
                    association,
                    url,
                    None,
                    request_kwargs={
                        "timeout": _IMAGE_FETCH_TIMEOUT,
                        "headers": self._build_fetch_headers(url),
                    },
                )
                self._mark_fetch_done()
                if _IMAGE_FETCH_SUCCESS_THROTTLE_SECONDS > 0:
                    time.sleep(_IMAGE_FETCH_SUCCESS_THROTTLE_SECONDS)

                self._image_assoc_cache[url] = {
                    "renditions": deepcopy(association.get("renditions")),
                    "mimetype": association.get("mimetype"),
                    "filemeta": deepcopy(association.get("filemeta")),
                    "filemeta_json": deepcopy(association.get("filemeta_json")),
                }
                return
            except Exception as ex:
                self._mark_fetch_done()
                last_error = ex
                if attempt == policy["retries"]:
                    break

                delay = (policy["base_backoff"] * attempt) + random.uniform(
                    0, _IMAGE_FETCH_JITTER_SECONDS
                )
                logger.warning(
                    "Image fetch failed for %s (attempt %s/%s), retrying in %.2fs: %s",
                    url,
                    attempt,
                    policy["retries"],
                    delay,
                    ex,
                )
                time.sleep(delay)

        if policy["failure_cooldown"] > 0:
            # After exhausting retries, cool down before the next image to reduce cascading failures.
            time.sleep(
                policy["failure_cooldown"]
                + random.uniform(0, _IMAGE_FETCH_JITTER_SECONDS)
            )

        raise last_error

    def _add_image(
        self, item, url, alt_text="", description_text="", is_featured=False
    ):
        """Fetch image, attach it as an association, and return the local storage href (or None)."""
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
        self._fetch_renditions_with_retry(association, url)

        if "featuremedia" not in associations:
            key = "featuremedia"
        else:
            key = "embedded" + str(len(associations) - 1)

        associations[key] = association

        return association.get("renditions", {}).get("original", {}).get("href")

    def _parse_feature_image(self, item, post):
        url = self._resolve_url(post.get("feature_image"))
        if url:
            try:
                self._add_image(item, url, is_featured=True)
            except Exception as e:
                logger.warning("Failed to fetch feature_image %s: %s", url, e)

    def _parse_inline_images(self, item, html):
        if not html:
            return
        try:
            root = parse_html(html, "html")
        except Exception as e:
            logger.warning("Failed to parse HTML for inline images: %s", e)
            return

        url_rewrites = {}
        for img in root.xpath(".//img"):
            try:
                raw_src = img.get("src")
                src = self._resolve_url(raw_src)
                if not src:
                    continue

                alt_text = img.get("alt") or ""
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

                local_href = self._add_image(item, src, alt_text, description_text)
                if local_href:
                    url_rewrites[src] = local_href
            except Exception as e:
                logger.warning(
                    "Failed to parse inline image %s: %s", img.get("src", "unknown"), e
                )

        if url_rewrites:
            body = item.get("body_html") or ""
            for external_url, local_href in url_rewrites.items():
                body = body.replace(external_url, local_href)
            item["body_html"] = body

    # ------------------------------------------------------------------
    # Date parsing
    # ------------------------------------------------------------------

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

    def _parse_post(self, post, authors_by_post, tags_by_post):
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

        html = post.get("html") or ""
        if self._ghost_url and html:
            html = html.replace(GHOST_URL_PLACEHOLDER, self._ghost_url)

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

        self._parse_feature_image(item, post)
        self._parse_inline_images(item, html)

        return item

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def iter_items(self, file_path, provider=None):
        """Parse a Ghost JSON export file and yield Superdesk items one at a time."""
        self._image_assoc_cache = {}
        self._last_image_fetch_ts = 0.0
        self._ghost_url = ((provider or {}).get("config", {}).get("url") or "").rstrip(
            "/"
        )
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

        # build lookup dicts
        users_by_id = {u["id"]: u for u in users}
        tags_by_id = {t["id"]: t for t in tags}

        authors_by_post = {}
        for pa in posts_authors:
            pid = pa.get("post_id")
            uid = pa.get("author_id")
            user = users_by_id.get(uid)
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
            tid = pt.get("tag_id")
            tag = tags_by_id.get(tid)
            if pid and tag:
                tags_by_post.setdefault(pid, []).append(
                    {
                        "name": tag.get("name", ""),
                        # Ghost slugs are the stable identifier, and the language
                        # tag is matched on either form.
                        "slug": tag.get("slug", ""),
                        # Coerce a null sort_order so the sort below can't blow
                        # up comparing None to an int.
                        "sort_order": pt.get("sort_order") or 0,
                    }
                )

        for post in posts:
            if post.get("status") != "published" or post.get("type") != "post":
                continue
            try:
                yield self._parse_post(post, authors_by_post, tags_by_post)
            except Exception as ex:
                logger.warning("Failed to parse Ghost post %s: %s", post.get("id"), ex)

    async def parse(self, file_path, provider=None):
        """Parse a Ghost JSON export file and return a list of Superdesk items.

        ``FeedParser.parse`` is a coroutine in the async core, so this override
        is ``async``. The parsing itself is synchronous (``iter_items``), so we
        simply materialise the generator here.
        """
        return list(self.iter_items(file_path, provider))


register_feed_parser(GhostParser.NAME, GhostParser())

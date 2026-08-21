"""URL and request-shape rules for Ghost images, free of superdesk imports.

Deliberately importable without an app: ``pesacheck/ingest/__init__.py`` pulls in
the parsers and therefore superdesk (and libmagic), which the Phase-0 tools under
``scripts/ghost/`` cannot rely on — they run against an export file on a laptop.
Keeping these rules here lets the parser and those tools share one definition
instead of hand-copying it.

That matters most for the headers. ``probe_image_host.py`` exists to measure what
an image host tolerates, and its answer only transfers if it sends what ingest
sends; a drifted copy yields a confidently wrong rate. The Referer in particular
has already caused one outage — see ``build_fetch_headers``.
"""

from urllib.parse import urlparse

# Ghost exports replace the site URL with this token in image and body URLs.
GHOST_URL_PLACEHOLDER = "__GHOST_URL__"

IMAGE_FETCH_HEADERS = {
    # Some CDNs are strict about clients and may reject the default python
    # user-agent. The Referer is deliberately NOT hardcoded here: it must match
    # each image's OWN origin and is set per-request in build_fetch_headers.
    # A hardcoded cross-site Referer (this used to be "https://medium.com/",
    # a leftover from when images were Medium-hosted) makes PesaCheck's own
    # Ghost site 403 every image — see build_fetch_headers for the full story.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) GhostIngest/1.0",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def build_fetch_headers(url):
    """Return image-fetch headers with a same-origin Referer.

    The Referer must match the image's OWN origin. PesaCheck's Ghost site
    (pesacheck.org) sits behind Cloudflare with a hotlink rule that returns 403
    for a cross-site Referer, so sending a Referer equal to the image's own
    scheme+host satisfies it for any source. A relative/hostless URL never
    reaches here (see resolve_url), but guard anyway and omit the Referer if we
    cannot build one.
    """
    headers = dict(IMAGE_FETCH_HEADERS)
    parsed = urlparse(url)
    if parsed.scheme and parsed.hostname:
        headers["Referer"] = f"{parsed.scheme}://{parsed.hostname}/"
    return headers


def resolve_url(url, ghost_url):
    """Turn a Ghost export URL into a fetchable absolute URL, or None.

    Substitutes the placeholder with the configured site URL and returns the
    result only if it is now absolute. Anything still relative (no configured
    site URL, or a genuinely relative link) is rejected rather than handed to
    the downloader, which would crash trying to resolve it against the app host.
    """
    if not url:
        return None
    if ghost_url:
        url = url.replace(GHOST_URL_PLACEHOLDER, ghost_url)
    if GHOST_URL_PLACEHOLDER in url or not urlparse(url).scheme:
        return None
    return url


def fetch_host(url):
    """Lowercased hostname, or "" — the rate-limiting and policy key."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_medium_cdn_url(url):
    return "medium.com" in fetch_host(url)


def is_ingestable_post(post):
    """Whether GhostParser will turn this export row into an item."""
    return post.get("status") == "published" and post.get("type") == "post"

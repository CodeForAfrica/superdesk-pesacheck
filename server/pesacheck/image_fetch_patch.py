"""Preserve the HTTP status of a failed image download.

Same shape as the other patches here (``media_patch``, ``publish_patch``,
``celery_eager_patch``): a defect in pinned superdesk-core that we cannot edit,
fixed by reassigning one function.

THE PROBLEM
-----------
``media_operations.download_file_from_url`` collapses every non-2xx response
into one generic message::

    rv = session.get(_get_url_for_request(url), **request_kwargs)
    if rv.status_code not in (200, 201):
        raise SuperdeskApiError.internalError(
            _("Failed to retrieve file from URL: {url}").format(url=url)
        )

The status code, the reason phrase and every response header are discarded. What
reaches the log is::

    500: Failed to retrieve file from URL: https://cdn-images-1.medium.com/...

which is the *same* line whether the host rate-limited us (429), blocked us
(403), fell over (503) or lost the image (404). Those want opposite responses —
back off and retry later, stop and fix the request, retry soon, give up
permanently — and we could not tell them apart during the Ghost backfill, where
image hosts throttle a bulk ingest.

``Retry-After`` matters just as much: when a host tells us how long to wait,
guessing instead is both slower and rude.

WHAT THIS DOES
--------------
Re-raises with the status, reason and the throttling headers in the message, and
attaches them to the exception (``pesacheck_status``, ``pesacheck_retry_after``)
so the Ghost fetch ladder can branch on them rather than parse a string. The
successful path is untouched: same call, same return value.

WHY BOTH MODULES
----------------
``renditions.py`` does ``from .media_operations import download_file_from_url``,
binding the function into its own namespace at import time, and that is the copy
our ingest path actually calls (via
``download_file_from_feeding_service_or_directly``). Patching only
``media_operations`` would leave the real call site on the original.
"""

import logging
from io import BytesIO

import requests

logger = logging.getLogger(__name__)

# Headers worth keeping: what a host uses to say "slow down" or "come back
# later". Lowercase because we match case-insensitively.
_RATE_LIMIT_HEADERS = (
    "retry-after",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-limit",
)


def _describe(response):
    """Human-readable status plus any rate-limit headers the host sent."""
    parts = ["HTTP %s" % response.status_code]
    reason = getattr(response, "reason", None)
    if reason:
        parts.append(str(reason))
    headers = getattr(response, "headers", None) or {}
    seen = {}
    for name, value in headers.items():
        if name.lower() in _RATE_LIMIT_HEADERS:
            seen[name] = value
    if seen:
        parts.append(
            "(" + ", ".join("%s=%s" % (k, v) for k, v in sorted(seen.items())) + ")"
        )
    return " ".join(parts)


def _retry_after(response):
    """``Retry-After`` in seconds, or None. Accepts the delta-seconds form only.

    The HTTP-date form is legal but rare from CDNs, and mis-parsing it into a
    huge sleep is worse than ignoring it.
    """
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.debug("Ignoring non-numeric Retry-After %r", raw)
        return None
    return seconds if seconds >= 0 else None


def apply():
    from superdesk.errors import SuperdeskApiError
    from superdesk.media import media_operations, renditions

    if getattr(media_operations, "_pesacheck_image_fetch_patched", False):
        return

    # Wrapping the original cannot work: it raises after discarding the
    # response, and re-requesting to learn the status would double the load on a
    # host that is already refusing us. So the few lines around the status check
    # are reimplemented here, calling exactly the same core helpers.
    def download_file_from_url(url, request_kwargs=None, session=None):
        request_kwargs = media_operations._set_default_request_headers(request_kwargs)
        own_session = session is None
        if own_session:
            session = requests.Session()
        try:
            response = session.get(
                media_operations._get_url_for_request(url), **request_kwargs
            )
            if response.status_code not in (200, 201):
                error = SuperdeskApiError.internalError(
                    "Failed to retrieve file from URL: %s (%s)"
                    % (url, _describe(response))
                )
                # Attached rather than parsed back out of the message: the Ghost
                # fetch ladder decides how long to wait from these.
                error.pesacheck_status = response.status_code
                error.pesacheck_retry_after = _retry_after(response)
                raise error
            content = BytesIO(response.content)
            name, content_type = (
                media_operations._get_name_and_content_type_from_response(
                    content, response.headers
                )
            )
            return content, name, content_type
        finally:
            if own_session:
                session.close()

    media_operations.download_file_from_url = download_file_from_url
    # The copy renditions.py bound at import time is the one our ingest calls.
    renditions.download_file_from_url = download_file_from_url
    media_operations._pesacheck_image_fetch_patched = True
    logger.info("Patched download_file_from_url to preserve the HTTP status")

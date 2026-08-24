"""Harden ``AmazonMediaStorage`` so a stalled aioboto3 S3 call cannot freeze ingest.

Root cause (proven 2026-08-18, see the superproject's
docs/postmortems/ghost-ingest-freeze.md, or Section 6A of the archived session log
alongside it for the full working):
~8% of aioboto3 S3 ``get_object`` calls hang **forever** on a stale keep-alive
connection, because superdesk-core builds the async S3 client with no read timeout
(``Config(signature_version="s3v4")``) and ``update_provider`` has no per-operation
timeout. The first stall (within the first ingest batch) freezes the whole provider
ingest until the 1800s ``soft_time_limit``, then the cycle repeats — so nothing ever
finishes ingesting. Local works only because it uses GridFS (no S3/aioboto3).

Fix applied here (pesacheck-owned because core is pinned to a moving ``@develop`` ref):
  1. Rebuild the S3 clients with real connect/read timeouts + botocore retries.
  2. Wrap ``call_async`` in ``asyncio.wait_for`` with a bounded retry that drops the
     (possibly stale) cached async client between attempts, so a hung connection is
     abandoned and reconnected instead of blocking ingest indefinitely.

Instrumentation proved a timeout+retry recovers: after a stalled call is cancelled,
subsequent S3 calls succeed. Applied once from ``app.get_app`` before the app (and thus
the ``AmazonMediaStorage`` singleton) is constructed.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# Tunables (env-overridable for ops).
S3_CONNECT_TIMEOUT = _int_env("S3_CONNECT_TIMEOUT", 10)
S3_READ_TIMEOUT = _int_env("S3_READ_TIMEOUT", 30)
S3_MAX_ATTEMPTS = _int_env("S3_MAX_ATTEMPTS", 3)  # botocore-level retries
# Per-attempt hard cap in call_async. Slightly above S3_READ_TIMEOUT so botocore's own
# timeout+retry can act first; wait_for is the backstop for when it doesn't (async path).
CALL_ASYNC_TIMEOUT = _int_env("S3_CALL_ASYNC_TIMEOUT", S3_READ_TIMEOUT + 5)
CALL_ASYNC_RETRIES = _int_env("S3_CALL_ASYNC_RETRIES", 3)


def apply():
    """Idempotently monkeypatch AmazonMediaStorage. Safe if S3 is unused (GridFS local)."""
    try:
        from botocore.client import Config
        from superdesk.storage import amazon_media_storage as _ams
    except Exception as exc:  # pragma: no cover - import guard
        logger.error(
            "media_patch: could not import AmazonMediaStorage, not patching: %r", exc
        )
        return

    cls = _ams.AmazonMediaStorage
    if getattr(cls, "_pesacheck_media_patched", False):
        return

    hardened_config = Config(
        signature_version="s3v4",
        connect_timeout=S3_CONNECT_TIMEOUT,
        read_timeout=S3_READ_TIMEOUT,
        retries={"max_attempts": S3_MAX_ATTEMPTS, "mode": "standard"},
        tcp_keepalive=True,
        max_pool_connections=25,
    )

    _orig_init = cls.__init__
    _orig_call_async = cls.call_async

    def __init__(self, app=None):
        _orig_init(self, app)
        # Core sets Config(signature_version="s3v4") with no timeouts, shared by both the
        # sync and (lazy) async clients. Override it and rebuild the sync client so both
        # get real timeouts + retries.
        try:
            import boto3

            self.connection_kwargs["config"] = hardened_config
            self.client = boto3.client("s3", **self.connection_kwargs)
        except Exception as exc:  # pragma: no cover - keep app boot resilient
            logger.error("media_patch: failed to harden sync S3 client: %r", exc)

    async def call_async(self, method, **kw):
        last_exc = None
        for attempt in range(1, CALL_ASYNC_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    _orig_call_async(self, method, **kw), timeout=CALL_ASYNC_TIMEOUT
                )
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "media_patch: S3 %s stalled >%ss (attempt %s/%s); dropping async client "
                    "and retrying",
                    method,
                    CALL_ASYNC_TIMEOUT,
                    attempt,
                    CALL_ASYNC_RETRIES,
                )
                _drop_async_client(self)
        raise TimeoutError(
            f"media_patch: S3 {method} stalled >{CALL_ASYNC_TIMEOUT}s after {CALL_ASYNC_RETRIES} attempts"
        ) from last_exc

    cls.__init__ = __init__
    cls.call_async = call_async
    cls._pesacheck_media_patched = True
    logger.info(
        "media_patch: AmazonMediaStorage hardened (connect=%ss read=%ss retries=%s "
        "call_async_timeout=%ss x%s)",
        S3_CONNECT_TIMEOUT,
        S3_READ_TIMEOUT,
        S3_MAX_ATTEMPTS,
        CALL_ASYNC_TIMEOUT,
        CALL_ASYNC_RETRIES,
    )


def _drop_async_client(storage):
    """Drop the cached aioboto3 client so the next call reconnects on a fresh connection.

    We do NOT await ``__aexit__`` on the stale client — that could itself block on the hung
    connection. Best-effort close it on the running loop as a detached task; the next
    call_async recreates the client from ``connection_kwargs``.
    """
    client = getattr(storage, "client_async", None)
    storage.client_async = None
    if client is None:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_safe_close(client))
    except Exception as exc:
        logger.error("media_patch: failed to drop async client: %r", exc)


async def _safe_close(client):
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5)
    except Exception as exc:
        logger.error("media_patch: failed to close client safely: %r", exc)

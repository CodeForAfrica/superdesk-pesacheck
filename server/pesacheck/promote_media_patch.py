"""Promote feature-media renditions out of the self-deleting ``temp/`` folder on publish.

Background (investigated 2026-08-19):
Images added in the editor are first uploaded via ``apps/archive/archive_media.py``
-> ``create_renditions_in_celery(...)`` with the default ``temporary=True``, so their
renditions land under the S3 ``temp`` folder (key ``superdesk/temp/<date>/<objectId>``)
and their media id embeds ``temp/``. A daily ``temp_files:gc`` beat task deletes anything
in ``folder=temp`` older than ``TEMP_FILE_EXPIRY_HOURS`` (24) — an application-level
delete, not an S3 lifecycle rule.

Body-embedded images escape this on their own: editing/cropping them re-renders through
``superdesk/media/media_editor.py`` (permanent), and ``editor_utils.generate_fields``
rebuilds ``body_html`` from those permanent hrefs. **Feature media is never re-rendered**,
so its ``featuremedia`` association keeps the ``temp/`` id through publish and is deleted
~24h later — and it never resolves through the media host + Cloudflare Worker, which maps
``/media/<date>_<id>`` -> ``superdesk/<date>/<id>`` and knows nothing about ``temp/``.

We deliberately do NOT make the initial upload permanent: the ``temp/`` + GC mechanism is
what reclaims genuinely abandoned (never-published) uploads. Instead, at publish time we
copy any still-``temp/`` rendition to its permanent key (the same ``<date>/<objectId>``,
just without the ``temp/`` prefix) and rewrite the rendition's ``media``/``href``. The
stale temp object is left for the normal GC to reclaim.

Scope: this only runs on publish and only touches renditions still under ``temp/`` — body
embeds that are already permanent are skipped, and the upload-time behaviour is untouched.
It also covers a media item's own ``renditions`` (a picture published as an associated
item). Idempotent and best-effort: any failure is logged and publish continues.

Applied once from ``app.get_app`` alongside media_patch / publish_patch. Inert on the
local GridFS stack, where media ids are Mongo ObjectIds and never carry a ``temp/`` prefix.
"""

import copy
import logging

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "temp/"


async def _promote_rendition(app, rend):
    """Copy one rendition's media out of temp/ to permanent, rewriting media + href.

    Returns True if the rendition was changed.
    """
    media = rend.get("media")
    if not media:
        return False
    media = str(media)
    if not media.startswith(_TEMP_PREFIX):
        return False

    permanent_id = media[len(_TEMP_PREFIX) :]  # "temp/<date>/<id>" -> "<date>/<id>"
    try:
        src = await app.media.get_async(media)
        if src is None:
            logger.warning(
                "promote_media: temp media %s not found; leaving temp id", media
            )
            return False
        buf = await src.to_buffer_sync()
        buf.seek(0)
        content_type = rend.get("mimetype") or getattr(src, "content_type", None)
        # put_async with an explicit _id and no folder stores at superdesk/<date>/<id>
        # (get_key adds the subfolder). Idempotent: if it already exists put is a no-op.
        await app.media.put_async(
            buf,
            filename=permanent_id.replace("/", "-"),
            content_type=content_type,
            _id=permanent_id,
        )
    except Exception:
        logger.exception(
            "promote_media: failed to promote %s -> %s; leaving temp id",
            media,
            permanent_id,
        )
        return False

    from superdesk.upload import url_for_media

    rend["media"] = permanent_id
    rend["href"] = url_for_media(permanent_id, content_type)
    return True


async def _promote_item_media(updates, original):
    from superdesk.core import get_current_app
    from superdesk.metadata.item import ASSOCIATIONS

    app = get_current_app()
    changed_any = False

    # 1. The item's own renditions (a media/picture item published as an associated item).
    for rend in list((updates.get("renditions") or {}).values()):
        if rend and await _promote_rendition(app, rend):
            changed_any = True

    # 2. Associations (featuremedia especially). Persist via `updates` so the promoted ids
    #    reach the stored + enqueued item; fall back to a copy of `original`'s associations
    #    when the publish payload didn't carry them, and assign it back only if changed.
    assocs = updates.get(ASSOCIATIONS)
    from_original = assocs is None
    if from_original:
        assocs = copy.deepcopy(original.get(ASSOCIATIONS) or {})

    assoc_changed = False
    for assoc in (assocs or {}).values():
        if not isinstance(assoc, dict):
            continue
        for rend in list((assoc.get("renditions") or {}).values()):
            if rend and await _promote_rendition(app, rend):
                assoc_changed = True

    if assoc_changed:
        updates[ASSOCIATIONS] = assocs
        changed_any = True

    if changed_any:
        logger.info("promote_media: promoted temp renditions to permanent on publish")


def apply():
    """Idempotently patch BasePublishService.on_update_async to promote temp media."""
    try:
        from apps.publish.content.common import BasePublishService
    except Exception as exc:
        logger.error(
            "promote_media_patch: could not import BasePublishService, not patching: %r",
            exc,
        )
        return

    if getattr(BasePublishService, "_pesacheck_promote_media", False):
        return

    _orig_on_update_async = BasePublishService.on_update_async

    async def on_update_async(self, updates, original):
        # Run core publish handling first so `updates` is fully populated (associations
        # refreshed, media-item fields set), then promote any still-temp renditions
        # before update_async persists and the item is enqueued for transmission.
        await _orig_on_update_async(self, updates, original)
        try:
            await _promote_item_media(updates, original)
        except Exception:  # pragma: no cover - never let promotion break a publish
            logger.exception(
                "promote_media_patch: promotion step failed; publish continues"
            )

    BasePublishService.on_update_async = on_update_async
    BasePublishService._pesacheck_promote_media = True
    logger.info(
        "promote_media_patch: feature-media temp->permanent promotion enabled on publish"
    )

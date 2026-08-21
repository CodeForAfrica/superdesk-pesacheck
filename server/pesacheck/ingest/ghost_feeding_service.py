import asyncio
import logging
import os
from itertools import islice

from celery.result import allow_join_result
from superdesk import get_resource_service
from superdesk.celery_app import celery
from superdesk.errors import ParserError
from superdesk.io.feeding_services.file_service import FileFeedingService
from superdesk.io.registry import register_feeding_service
from superdesk.metadata.item import CONTENT_STATE, GUID_FIELD
from superdesk.notification import push_notification
from superdesk.resource_fields import ID_FIELD
from superdesk.utils import FileSortAttributes, get_sorted_files

from pesacheck.ingest.util import env_float, env_int

logger = logging.getLogger(__name__)

BATCH_SIZE = env_int("GHOST_INGEST_BATCH_SIZE", 10)

# Desk the ingested fact-checks are fetched onto on their way to being
# published. Publisher assigns the route from the article's language, not the
# desk, so the desk is only a required waypoint in Superdesk's publish workflow;
# override it with the provider config ``publish_desk``.
DEFAULT_PUBLISH_DESK = "Newsdesk"

# Hard ceiling on a single item's fetch+publish. Publishing is decoupled from
# ingest (see below), but a stuck publish must still be bounded so one bad item
# cannot pin a worker slot forever — it is logged and skipped instead.
PUBLISH_ITEM_TIMEOUT = env_float("GHOST_PUBLISH_ITEM_TIMEOUT", 120)


class GhostFeedingService(FileFeedingService):
    """
    Feeding Service for Ghost CMS JSON export files.

    Extends the standard file feeding service to yield ingested items
    in batches of BATCH_SIZE, making large Ghost dumps manageable.
    """

    NAME = "ghost_file"
    label = "Ghost CMS file feed"

    # Extend the base file-feed config (the "Server Folder" path) with the Ghost
    # site URL, used by GhostParser to resolve the __GHOST_URL__ placeholder in
    # exported image/body URLs so images can be fetched.
    fields = FileFeedingService.fields + [
        {
            "id": "url",
            "type": "text",
            "label": "Ghost site URL",
            "placeholder": "https://your-ghost-site",
            "required": False,
        }
    ]

    async def _update(self, provider, update):
        """Yield ingested items in batches, dispatching publish out-of-band.

        Ported to the async ingest framework: this is now an async generator and
        the file-system / parser helpers (``get_sorted_files``, ``get_feed_parser``,
        ``is_empty``, ``move_file``) are awaited. The old two-way ``failed = yield``
        protocol is gone — ``FeedingService.update`` only iterates items forward —
        so a file is treated as successfully consumed unless an exception is raised.

        Publishing is NOT done inline. Each stored batch is handed to the
        ``publish_ghost_batch`` celery task via ``send_task`` (fire-and-forget),
        so a slow or stuck publish can never block ingestion. Doing it inline
        coupled ingest liveness to the publish path: a single hung publish (e.g.
        the blocking celery join when publishing an item with an image) froze the
        whole provider ingest with no error logged. Decoupled, ingest runs to
        completion regardless and the batches publish incrementally in the
        background (concurrently with ingest when the worker has a free slot,
        otherwise straight after it drains the queue).
        """
        self.provider = provider
        self.path = provider.get("config", {}).get("path", None)
        provider_config = provider.get("config", {})

        if not self.path:
            logger.warning(
                "Ghost Feeding Service %s is configured without path. Please check the configuration",
                provider["name"],
            )
            return

        for filename in await get_sorted_files(
            self.path, sort_by=FileSortAttributes.created
        ):
            last_updated = None
            try:
                file_path = os.path.join(self.path, filename)
                if not os.path.isfile(file_path):
                    continue

                last_updated = self.get_last_updated(file_path)

                if not self.is_latest_content(
                    last_updated, provider.get("last_updated")
                ):
                    await self.move_file(
                        self.path, filename, provider=provider, success=False
                    )
                    continue

                if await self.is_empty(file_path):
                    logger.info("Ignoring empty file %s", filename)
                    continue

                parser = await self.get_feed_parser(provider, file_path)
                if not parser.can_parse(file_path):
                    logger.info("Skipping non-Ghost file %s", filename)
                    continue

                items_gen = parser.iter_items(file_path, provider)

                pending_guids = []
                while True:
                    batch = list(islice(items_gen, BATCH_SIZE))
                    if not batch:
                        break

                    # The consumer stores each yielded batch before resuming us
                    # for the next one, so by the time we're back here the
                    # PREVIOUS batch is already in the ingest collection and its
                    # guids are safe to hand off for publishing. Dispatching one
                    # step behind the yield guarantees the task never races ahead
                    # of storage.
                    if pending_guids:
                        await _dispatch_publish(provider_config, pending_guids)

                    pending_guids = [
                        item[GUID_FIELD] for item in batch if item.get(GUID_FIELD)
                    ]
                    yield batch

                # The final batch was stored when the consumer resumed us past
                # the last yield (the break above), so dispatch it before moving
                # the file.
                if pending_guids:
                    await _dispatch_publish(provider_config, pending_guids)

                await self.move_file(
                    self.path, filename, provider=provider, success=True
                )

            except Exception as ex:
                if last_updated and self.is_old_content(last_updated):
                    await self.move_file(
                        self.path, filename, provider=provider, success=False
                    )
                raise ParserError.parseFileError(
                    "{}-{}".format(provider["name"], self.NAME), filename, ex, provider
                )

        push_notification("ingest:update")


async def _dispatch_publish(provider_config, guids):
    """Queue a batch for background publishing; never raise into the ingest loop.

    Enqueued with ``celery.send_task`` rather than ``publish_ghost_batch.apply_async``
    — deliberately, and NOT interchangeable here:

    * ``HybridAppContextWorkerTask.apply_async`` is an ``async def``, so calling it
      without ``await`` builds a coroutine that is never run and the task is never
      enqueued at all (Python only whispers a "coroutine was never awaited"
      RuntimeWarning, and the ``except`` below can never fire).
    * Awaiting it is no better: its ``_is_always_eager()`` returns True for any task
      that is not itself mid-execution (``self.request.id`` is None off the worker),
      so it takes the eager branch and calls ``.get()`` on the result. With
      ``CELERY_RESULT_BACKEND`` set — as it is in every deployed env — that blocks
      the ingest loop on the batch it just queued, which is exactly the coupling
      this indirection exists to remove.

    ``send_task`` is a plain synchronous broker publish (a quick Redis call) with no
    result wait, so it returns immediately. Any failure to enqueue is logged and
    swallowed — losing a publish must never abort the surrounding ingest.
    """
    if not guids:
        return

    # Under ``CELERY_TASK_ALWAYS_EAGER`` (tests, some local setups) nothing drains
    # the broker, so ``send_task`` would silently strand the batch. Publish inline
    # instead — bounded per item exactly as the task body is.
    if celery.conf.task_always_eager:
        await _publish_guids({"config": provider_config or {}}, guids)
        return

    try:
        celery.send_task(
            publish_ghost_batch.name,
            args=(provider_config, guids),
            ignore_result=True,
        )
        logger.info("Ghost auto-publish: queued batch of %d", len(guids))
    except Exception:
        logger.exception(
            "Ghost auto-publish: failed to enqueue batch of %d", len(guids)
        )


@celery.task(soft_time_limit=1800, ignore_result=True)
async def publish_ghost_batch(provider_config, guids):
    """Fetch a batch of freshly ingested items onto a desk and publish them.

    Runs in its own celery task, decoupled from ingest, so a stuck publish only
    pins this task (bounded per item by ``PUBLISH_ITEM_TIMEOUT``) instead of the
    whole provider ingest. Takes the provider ``config`` dict (JSON-serializable —
    just the ``auto_publish`` flag and ``publish_desk`` name matter) rather than a
    provider id, avoiding an id round-trip and any ObjectId serialization quirk.
    """
    await _publish_guids({"config": provider_config or {}}, guids)


async def _publish_guids(provider, guids):
    """Fetch each freshly ingested item onto a desk and publish it.

    Publishing is what carries an item through the http_push destination to
    Superdesk Publisher, where the language rules drop it onto the matching
    route — so with this in place ingest is hands-off end to end. Failures
    are logged per item and never abort the batch: a story that will not
    validate simply stays unpublished for an editor to finish by hand.
    """
    if not guids:
        return
    if not provider.get("config", {}).get("auto_publish", True):
        return

    desk_id, stage_id = await _resolve_publish_target(provider)
    if desk_id is None:
        logger.warning(
            "Ghost auto-publish skipped: no desk to fetch onto "
            "(set the provider config 'publish_desk')"
        )
        return

    ingest_service = get_resource_service("ingest")
    fetch_service = get_resource_service("fetch")
    publish_service = get_resource_service("archive_publish")

    # One ``$in`` query for the whole batch instead of a find_one per guid.
    cursor = await ingest_service.get_from_mongo_async(
        req=None, lookup={GUID_FIELD: {"$in": guids}}
    )
    items_by_guid = {item[GUID_FIELD]: item async for item in cursor}

    for guid in guids:
        try:
            ingest_item = items_by_guid.get(guid)
            if not ingest_item:
                continue
            # ``archived`` is stamped by the fetch service, so a set value
            # means this item was already fetched — don't publish it twice.
            if ingest_item.get("archived"):
                continue

            await asyncio.wait_for(
                _publish_one(
                    fetch_service, publish_service, ingest_item, desk_id, stage_id
                ),
                timeout=PUBLISH_ITEM_TIMEOUT,
            )
            logger.info("Ghost auto-publish: published %s", guid)
        except asyncio.TimeoutError:
            logger.error(
                "Ghost auto-publish timed out after %ss for %s; skipping",
                PUBLISH_ITEM_TIMEOUT,
                guid,
            )
        except Exception:
            logger.exception("Ghost auto-publish failed for %s", guid)


async def _publish_one(fetch_service, publish_service, ingest_item, desk_id, stage_id):
    """Fetch one ingest item onto the desk and publish it."""
    fetch_doc = {
        ID_FIELD: ingest_item[ID_FIELD],
        "desk": str(desk_id),
        "state": CONTENT_STATE.ROUTED,
    }
    if stage_id:
        fetch_doc["stage"] = str(stage_id)

    archive_id = (await fetch_service.fetch([fetch_doc]))[0]
    # ``allow_join_result`` is required because publishing an item with an image
    # cascades to publishing the picture association, which matches a
    # ``polling=True`` publish channel; that path calls
    # ``enqueue_published.apply_async()`` and then ``.get()`` on the result.
    # Celery forbids ``.get()`` inside a running task, so this scopes the join
    # guard off just for the publish. (Text-only items take a ``polling=False``
    # path and never hit ``.get()``, which is why image-free items publish fine
    # without this.)
    with allow_join_result():
        await publish_service.patch_async(archive_id, {"auto_publish": True})


async def _resolve_publish_target(provider):
    """Return the ``(desk_id, stage_id)`` to fetch onto, or ``(None, None)``."""
    desks_service = get_resource_service("desks")
    desk_name = provider.get("config", {}).get("publish_desk") or DEFAULT_PUBLISH_DESK
    desk = await desks_service.find_one_async(req=None, name=desk_name)
    if not desk:
        desk = await desks_service.find_one_async(req=None)
    if not desk:
        return None, None
    return desk[ID_FIELD], desk.get("incoming_stage")


register_feeding_service(GhostFeedingService)

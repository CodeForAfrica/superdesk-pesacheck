import os
import logging
from itertools import islice

from celery.result import allow_join_result

from superdesk import get_resource_service
from superdesk.errors import ParserError
from superdesk.io.feeding_services.file_service import FileFeedingService
from superdesk.io.registry import register_feeding_service
from superdesk.metadata.item import CONTENT_STATE, GUID_FIELD
from superdesk.notification import push_notification
from superdesk.resource_fields import ID_FIELD
from superdesk.utils import get_sorted_files, FileSortAttributes


logger = logging.getLogger(__name__)

BATCH_SIZE = 10

# Desk the ingested fact-checks are fetched onto on their way to being
# published. Publisher assigns the route from the article's language, not the
# desk, so the desk is only a required waypoint in Superdesk's publish workflow;
# override it with the provider config ``publish_desk``.
DEFAULT_PUBLISH_DESK = "Newsdesk"


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
        """Yield ingested items in batches.

        Ported to the async ingest framework: this is now an async generator and
        the file-system / parser helpers (``get_sorted_files``, ``get_feed_parser``,
        ``is_empty``, ``move_file``) are awaited. The old two-way ``failed = yield``
        protocol is gone — ``FeedingService.update`` only iterates items forward —
        so a file is treated as successfully consumed unless an exception is raised.
        """
        self.provider = provider
        self.path = provider.get("config", {}).get("path", None)

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

                file_guids = []
                while True:
                    batch = list(islice(items_gen, BATCH_SIZE))
                    if not batch:
                        break
                    file_guids.extend(
                        item[GUID_FIELD] for item in batch if item.get(GUID_FIELD)
                    )
                    yield batch

                await self.move_file(
                    self.path, filename, provider=provider, success=True
                )

                # The consumer stores each batch before asking for the next, so
                # once the file is moved every item above is in the ingest
                # collection and can be fetched onto a desk and published.
                await self._auto_publish(provider, file_guids)

            except Exception as ex:
                if last_updated and self.is_old_content(last_updated):
                    await self.move_file(
                        self.path, filename, provider=provider, success=False
                    )
                raise ParserError.parseFileError(
                    "{}-{}".format(provider["name"], self.NAME), filename, ex, provider
                )

        push_notification("ingest:update")

    async def _auto_publish(self, provider, guids):
        """Fetch each freshly ingested item onto a desk and publish it.

        Publishing is what carries an item through the http_push destination to
        Superdesk Publisher, where the language rules drop it onto the matching
        route — so with this in place ingest is hands-off end to end. Failures
        are logged per item and never abort the ingest: a story that will not
        validate simply stays unpublished for an editor to finish by hand.
        """
        if not guids:
            return
        if not provider.get("config", {}).get("auto_publish", True):
            return

        desk_id, stage_id = await self._resolve_publish_target(provider)
        if desk_id is None:
            logger.warning(
                "Ghost auto-publish skipped: no desk to fetch onto "
                "(set the provider config 'publish_desk')"
            )
            return

        ingest_service = get_resource_service("ingest")
        fetch_service = get_resource_service("fetch")
        publish_service = get_resource_service("archive_publish")

        for guid in guids:
            try:
                ingest_item = await ingest_service.find_one_async(req=None, guid=guid)
                if not ingest_item:
                    continue
                # ``archived`` is stamped by the fetch service, so a set value
                # means this item was already fetched — don't publish it twice.
                if ingest_item.get("archived"):
                    continue

                fetch_doc = {
                    ID_FIELD: ingest_item[ID_FIELD],
                    "desk": str(desk_id),
                    "state": CONTENT_STATE.ROUTED,
                }
                if stage_id:
                    fetch_doc["stage"] = str(stage_id)

                archive_id = (await fetch_service.fetch([fetch_doc]))[0]
                # ``allow_join_result`` is required because we publish from
                # inside the ``update_provider`` celery task. Publishing an item
                # with an image cascades to publishing the picture association,
                # which matches a ``polling=True`` publish channel; that path
                # calls ``enqueue_published.apply_async()`` and then ``.get()``
                # on the (eager) result. Celery forbids ``.get()`` inside a
                # running task, but here the subtask runs eagerly in-process, so
                # there is no remote worker to block on — this scopes the join
                # guard off just for the publish. (Text-only items take a
                # ``polling=False`` path and never hit ``.get()``, which is why
                # image-free items published fine without this.)
                with allow_join_result():
                    await publish_service.patch_async(
                        archive_id, {"auto_publish": True}
                    )
                logger.info("Ghost auto-publish: published %s", guid)
            except Exception:
                logger.exception("Ghost auto-publish failed for %s", guid)

    async def _resolve_publish_target(self, provider):
        """Return the ``(desk_id, stage_id)`` to fetch onto, or ``(None, None)``."""
        desks_service = get_resource_service("desks")
        desk_name = (
            provider.get("config", {}).get("publish_desk") or DEFAULT_PUBLISH_DESK
        )
        desk = await desks_service.find_one_async(req=None, name=desk_name)
        if not desk:
            desk = await desks_service.find_one_async(req=None)
        if not desk:
            return None, None
        return desk[ID_FIELD], desk.get("incoming_stage")


register_feeding_service(GhostFeedingService)

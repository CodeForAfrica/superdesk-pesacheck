import os
import logging
from itertools import islice

from superdesk.errors import ParserError
from superdesk.io.feeding_services.file_service import FileFeedingService
from superdesk.io.registry import register_feeding_service
from superdesk.notification import push_notification
from superdesk.utils import get_sorted_files, FileSortAttributes


logger = logging.getLogger(__name__)

BATCH_SIZE = 10


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

        for filename in await get_sorted_files(self.path, sort_by=FileSortAttributes.created):
            last_updated = None
            try:
                file_path = os.path.join(self.path, filename)
                if not os.path.isfile(file_path):
                    continue

                last_updated = self.get_last_updated(file_path)

                if not self.is_latest_content(last_updated, provider.get("last_updated")):
                    await self.move_file(self.path, filename, provider=provider, success=False)
                    continue

                if await self.is_empty(file_path):
                    logger.info("Ignoring empty file %s", filename)
                    continue

                parser = await self.get_feed_parser(provider, file_path)
                if not parser.can_parse(file_path):
                    logger.info("Skipping non-Ghost file %s", filename)
                    continue

                items_gen = parser.iter_items(file_path, provider)

                while True:
                    batch = list(islice(items_gen, BATCH_SIZE))
                    if not batch:
                        break
                    yield batch

                await self.move_file(self.path, filename, provider=provider, success=True)

            except Exception as ex:
                if last_updated and self.is_old_content(last_updated):
                    await self.move_file(self.path, filename, provider=provider, success=False)
                raise ParserError.parseFileError("{}-{}".format(provider["name"], self.NAME), filename, ex, provider)

        push_notification("ingest:update")


register_feeding_service(GhostFeedingService)

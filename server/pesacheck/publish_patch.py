"""Fix the stale-etag 412 in superdesk-core's async publish consumer.

Root cause (proven 2026-08-18): ``AsyncioPublishConsumer.transmit_item`` runs two
sequential ``PublishQueueResource.get_service().update(...)`` calls on the SAME
``task`` object:

  1. an IN_PROGRESS update (before transmit), and
  2. a SUCCESS update (after transmit) — or a RETRYING/FAILED update in the except.

``update()`` returns a fresh clone carrying the new ``_etag`` but the consumer
discards it, so ``task.etag`` still holds the pre-(1) value. Step (2) reuses that
stale etag; ``update_in_dbs`` builds a Mongo query ``{_id, _etag: <stale>}`` that
matches 0 rows and raises ``412 Client and server etags don't match``. The except
handler then reuses the same stale etag for the failure-state write and 412s again
("Failed to set the state for failed publish queue item"), so the queue item is
orphaned in IN_PROGRESS.

The transmit itself (the http_push to Publisher) happens BEFORE the failing
success-update, and the transmitter calls ``raise_for_status()`` — so a bad push
would surface from ``transmitter.transmit()``, not from the queue update. The 412
therefore does NOT stop delivery; it only corrupts the queue bookkeeping and spams
logs, and (because the retry-state write also 412s) suppresses auto-retry of
genuinely transient transmit failures.

Fix (pesacheck-owned because core is pinned to a moving ``@develop`` ref, same as
``media_patch``): reimplement ``transmit_item`` reassigning ``task`` from each
``update()`` return, so every subsequent update uses the current etag. Behaviour is
otherwise identical to the upstream method. Applied once from ``app.get_app`` before
the app is built.

NOTE: this overrides the vendored ``transmit_item`` wholesale, so on a core bump
re-diff it against
``superdesk/publish_async/consumers/asyncio_consumer.py`` and drop this patch if
upstream captures the clone.
"""

import logging
from datetime import timedelta
from inspect import isawaitable

logger = logging.getLogger(__name__)


def apply():
    """Idempotently monkeypatch AsyncioPublishConsumer.transmit_item."""
    try:
        from superdesk.core import get_config
        from superdesk.errors import PublishHTTPPushClientError
        from superdesk.lifecycle_timing import (
            duration_ms,
            duration_ms_from_epoch,
            to_epoch_ms,
        )
        from superdesk.publish import registered_transmitters
        from superdesk.publish_async.consumers.asyncio_consumer import (
            AsyncioPublishConsumer,
        )
        from superdesk.publish_async.utils import compute_retry_timeout_minutes
        from superdesk.resource_fields import LAST_UPDATED
        from superdesk.types import PublishQueueResource, PublishQueueState
        from superdesk.utc import utcnow
    except Exception as exc:  # pragma: no cover - import guard
        logger.error(
            "publish_patch: could not import async publish consumer, not patching: %r",
            exc,
        )
        return

    if getattr(AsyncioPublishConsumer, "_pesacheck_etag_patched", False):
        return

    async def transmit_item(self, task) -> bool:
        log_msg = (
            f"_id: {task.id} item_id: {task.item_id} state: {task.state} "
            f"item_version: {task.item_version} headline: {task.headline}"
        )
        log_extra = dict(
            task_id=task.id,
            task_state=task.state,
            item_id=task.item_id,
            item_version=task.item_version,
            item_headline=task.headline,
        )
        if task.state not in [
            PublishQueueState.ROUTING,
            PublishQueueState.PENDING,
            PublishQueueState.RETRYING,
        ]:
            logger.warning(
                "Transmit State is not pending/retrying for queue item "
                "(task_id=%s item_id=%s item_version=%s state=%s)",
                task.id,
                task.item_id,
                task.item_version,
                task.state,
                extra=log_extra,
            )
            return False
        elif task.destination is None:
            logger.error("Destination not defined in queue item", extra=log_extra)
            return False

        try:
            # Update the status of the task to in-progress.
            # PATCH: reassign ``task`` to the returned clone so its etag is refreshed
            # for the subsequent success/failure update (upstream discards this).
            task_update = {
                "state": PublishQueueState.IN_PROGRESS,
                "transmit_started_at": utcnow(),
            }
            task = await PublishQueueResource.get_service().update(
                task.id, task_update, task.etag, task
            )
            logger.info(f"Transmitting queue item {log_msg}")

            transmitter = registered_transmitters[task.destination.delivery_type]

            response = transmitter.transmit(
                task.to_dict(context={"use_objectid": True})
            )
            if isawaitable(response):
                await response

            completed_now = utcnow(microseconds=True)
            completed_at = completed_now.replace(microsecond=0)
            completed_ms = to_epoch_ms(completed_now)
            success_update = {
                "state": PublishQueueState.SUCCESS,
                "completed_at": completed_at,
                "completed_ms": completed_ms,
            }
            if isinstance(task.lifecycle_started_ms, int):
                success_update["lifecycle_to_transmit_ms"] = duration_ms_from_epoch(
                    task.lifecycle_started_ms, completed_ms
                )
            elif task.lifecycle_started_at:
                success_update["lifecycle_to_transmit_ms"] = duration_ms(
                    task.lifecycle_started_at, completed_now
                )

            # PATCH: uses the refreshed etag from the IN_PROGRESS update above.
            task = await PublishQueueResource.get_service().update(
                task.id, success_update, task.etag, task
            )
            logger.info(f"Transmit completed for queue item {log_msg}")

            return True
        except Exception as e:
            logger.exception("Failed to transmit queue item", extra=log_extra)

            max_retry_attempt = get_config(int, "MAX_TRANSMIT_RETRY_ATTEMPT")
            initial_retry_delay_minutes = get_config(
                int,
                "TRANSMIT_RETRY_INITIAL_DELAY_MINUTES",
                get_config(int, "TRANSMIT_RETRY_ATTEMPT_DELAY_MINUTES", 1),
            )
            max_retry_delay_minutes = get_config(
                int,
                "TRANSMIT_RETRY_MAX_DELAY_MINUTES",
                get_config(int, "MAX_TRANSMIT_RETRY_DELAY_MINUTES", 120),
            )
            try:
                retry_attempt = task.retry_attempt or 0
                timeout_minutes = compute_retry_timeout_minutes(
                    retry_attempt,
                    initial_retry_delay_minutes,
                    max_retry_delay_minutes,
                )
                updates = {LAST_UPDATED: utcnow()}

                if task.retry_attempt < max_retry_attempt and not isinstance(
                    e, PublishHTTPPushClientError
                ):
                    updates.update(
                        {
                            "retry_attempt": task.retry_attempt + 1,
                            "state": PublishQueueState.RETRYING,
                            "next_retry_attempt_at": utcnow()
                            + timedelta(minutes=timeout_minutes),
                        }
                    )
                else:
                    updates["state"] = PublishQueueState.FAILED

                # PATCH: ``task`` carries the etag from whichever update last succeeded
                # (the IN_PROGRESS one when transmit failed), so this failure-state
                # write no longer 412s on a stale etag.
                await PublishQueueResource.get_service().update(
                    task.id, updates, task.etag, task
                )
                return False
            except Exception:
                logger.error(
                    "Failed to set the state for failed publish queue item.",
                    extra=log_extra,
                )

            logger.debug(f"Got error. {log_msg}")
            raise

    AsyncioPublishConsumer.transmit_item = transmit_item
    AsyncioPublishConsumer._pesacheck_etag_patched = True
    logger.info(
        "publish_patch: AsyncioPublishConsumer.transmit_item etag refresh applied"
    )

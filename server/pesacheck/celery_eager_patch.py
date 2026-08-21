"""Stop ``apply_async`` from blocking on a subtask it just queued.

Same shape as the other patches here (``media_patch``, ``publish_patch``,
``promote_media_patch``): a defect in pinned superdesk-core that we cannot edit,
fixed by reassigning one method.

THE BUG
-------
``HybridAppContextTask._is_always_eager`` decides whether ``apply_async`` should
run a task inline or hand it to the broker::

    def _is_always_eager(self):
        current_request = getattr(self, "request", None)
        if current_request is None or not getattr(current_request, "id", None):
            return True                      # <-- fires for every dispatch
        ...
        eager_value = getattr(task_conf, "task_always_eager", None)
        if eager_value is not None:
            return bool(eager_value)         # <-- the correct answer, unreachable

``self.request`` is bound only while a task is *executing*. Dispatching a task
means calling ``apply_async`` on one that is not, so ``request.id`` is ``None``
and the guard returns True for every send — regardless of
``CELERY_TASK_ALWAYS_EAGER``. The canonical conf check below it, which the
comment says is preferred, is unreachable in exactly the case it was added for.

``apply_async`` then takes its eager branch::

    if self._is_always_eager():
        async_result = super().apply_async(...)   # conf is False -> goes to the BROKER
        eager_result = async_result.get()         # ...then blocks on the result

Celery's own ``apply_async`` honours the real conf, so the message is genuinely
queued; the ``.get()`` then waits for a worker to run it.

WHAT IT COST US
---------------
Three failure modes, all this one bug:

1. ``update_ingest`` -> ``update_provider.apply_async()`` raised
   ``RuntimeError: Never call result.get() within a task!`` and died. The
   provider had already been queued, so ingest ran and the error looked cosmetic.

2. Ghost auto-publish never fired at all, because ``_dispatch_publish`` called
   ``apply_async`` without awaiting the coroutine. Worked around there with
   ``celery.send_task``; see ghost_feeding_service._dispatch_publish.

3. A deadlock that published exactly one article. ``publish_ghost_batch`` filled
   all three worker slots; each published an item, which cascades to
   ``enqueue_published.apply_async()``, which queued the subtask and blocked on
   ``.get()`` — with no slot left to run it. 70 ``enqueue_published`` messages
   backed up behind three publishers waiting on them. ``allow_join_result()``
   had suppressed the guard from (1), so it blocked silently instead of raising,
   and ``asyncio.wait_for`` could not time it out because ``.get()`` blocks the
   thread rather than yielding to the event loop.

THE FIX
-------
Consult ``task_always_eager`` first and use the request-binding heuristic only
as the fallback it was meant to be. Non-eager dispatch then returns an
``AsyncResult`` without waiting, which is the contract core's own callers
already expect: the two that consume a return value both branch on
``isinstance(result, AsyncTaskResult)`` and poll the non-blocking
``get_result_async()`` (see ``media/renditions.py`` and ``media/media_editor.py``);
every other call site discards it.

Note this restores *asynchronous* publish enqueueing. Previously the blocking
``.get()`` meant a publish waited for its enqueue to finish — accidental, not
designed, and only ever survivable while one slot stayed free.

Re-check on every superdesk-core bump: if upstream reorders these checks, drop
this module.
"""

import logging

logger = logging.getLogger(__name__)


def apply():
    from superdesk.celery_app.context_task import HybridAppContextTask

    if getattr(HybridAppContextTask, "_pesacheck_eager_patched", False):
        return

    def _is_always_eager(self):
        # Celery's canonical flag is the authority: it is what its own
        # apply_async consults to decide whether to run inline or publish to the
        # broker, so anything else here can only disagree with reality.
        task_app = getattr(self, "app", None)
        task_conf = getattr(task_app, "conf", None) if task_app is not None else None
        eager_value = (
            getattr(task_conf, "task_always_eager", None)
            if task_conf is not None
            else None
        )
        if eager_value is not None:
            return bool(eager_value)

        try:
            app = self.get_current_app()
            config_value = app.config.get("CELERY_TASK_ALWAYS_EAGER")
        except Exception:
            config_value = None
        if config_value is not None:
            return bool(config_value)

        # Fallback only: with no conf available, an unbound request means the
        # task was invoked directly rather than via delay/apply_async, so
        # running it inline is the sensible default. This is upstream's original
        # test, demoted from first to last.
        current_request = getattr(self, "request", None)
        return current_request is None or not getattr(current_request, "id", None)

    HybridAppContextTask._is_always_eager = _is_always_eager
    HybridAppContextTask._pesacheck_eager_patched = True
    logger.info(
        "celery_eager_patch: _is_always_eager now honours task_always_eager, "
        "so apply_async no longer blocks on a subtask it queued"
    )

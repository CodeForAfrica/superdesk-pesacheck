"""Load the tracked content config from `server/data/` the way it needs loading.

The tracked content config now lives under `server/data/` (core's `INIT_DATA_PATH`),
one file per collection — except `vocabularies`, which is split into `data/vocabularies/editorial/*.json`
and `data/vocabularies/reference/*.json`, one file per vocabulary, so a change to
one vocabulary is a one-file diff.

Core's `app:initialize_data` loads that config, but two collection groups need
help, so this patch wraps `apps.prepopulate.app_initialize.import_file`:

1. **`vocabularies` — the split directory.** Core expects a single
   `vocabularies.json` per data path; faced with the split directory it finds no
   such file and silently falls back to its own stock `vocabularies.json` (24
   generic vocabularies). We glob the directory, drop the deny-listed
   vocabularies, concatenate the rest into a temporary `vocabularies.json`, and
   delegate to the original `import_file`. Everything else — `do_patch`,
   `init_version`/`--force` gating, `_deleted` tombstones, ETAG, indexes — is
   core's, unchanged.

   Deny-list: `keywords` is never seeded from a file. It accumulates at runtime
   on publish (`KEYWORDS_ADD_MISSING_ON_PUBLISH`, on in `settings.py`); seeding
   it would reset an accumulating collection. `VOCABULARY_DENY_LIST` is the
   enforcer of that invariant.

2. **`desks` / `stages` / `content_templates` — raw drop-and-load.** These are
   load-once (`do_patch=False`) and, crucially, their services fire side-effects
   on insert: creating a desk auto-creates stages and runs "Updating Stage
   Visibility", which resets `stage.desk_order` and duplicates
   `content_template.template_desks`. The old tgz path inserted raw BSON and
   bypassed all of that; a round-trip through `service.post` does not. So for
   these three we replicate the old behaviour: read the tracked JSON, `_mongotize`
   it (hex strings -> ObjectId), drop the collection, and `insert_many` straight
   into Mongo, bypassing the service hooks. `reassign_ownership` (in the seed)
   then re-stamps ownership and desk membership afterwards. This is the plan's
   "keep a drop step" decision for the load-once collections.

What would invalidate this patch: core no longer routing these entities through
`import_file(entity_name, path, file_name, ...)`, renaming an entity, or changing
`_mongotize`. Re-check `apps/prepopulate/app_initialize.py` on a core bump. It is
a no-op (falls through to core) when a tracked file/directory is absent, so it is
safe to keep applied during the transition.
"""

import json
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Vocabularies that must never be seeded from a tracked file (runtime-accumulated).
VOCABULARY_DENY_LIST = {"keywords"}

# Load-once collections whose service hooks mutate the data on insert.
RAW_LOAD_ENTITIES = {"desks", "stages", "content_templates"}


def _load_split_vocabularies(vocab_dir):
    """Every vocabulary doc under `vocab_dir`, deny-listed ones removed."""
    docs = []
    for path in sorted(vocab_dir.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "_id" not in doc:
            logger.warning("content_config: skipping non-vocabulary file %s", path)
            continue
        if doc["_id"] in VOCABULARY_DENY_LIST:
            logger.info(
                "content_config: deny-listed vocabulary %s not seeded", doc["_id"]
            )
            continue
        docs.append(doc)
    return docs


async def _raw_drop_load(entity_name, file_path, index_params, original_import_file):
    """Drop the collection and insert the tracked docs raw, bypassing hooks.

    Indexes are (re)created by delegating to core's own `import_file` with no file
    (`file_name=None`): core then skips the load and runs only its index-creation
    tail, so a core change to index handling is picked up automatically instead of
    drifting from a copied loop (AGENTS.md §5, "delegate, don't copy a core body").
    """
    import superdesk
    from superdesk.core import get_current_app

    app = get_current_app()
    service = superdesk.get_resource_service(entity_name)
    docs = json.loads(file_path.read_text(encoding="utf-8"))
    docs = [app.data.mongo._mongotize(doc, service.datasource) for doc in docs]
    collection = app.data.mongo.pymongo(resource=entity_name).db[entity_name]
    collection.drop()
    if docs:
        collection.insert_many(docs)
    await original_import_file(entity_name, None, None, index_params)
    logger.info(
        "content_config: raw-loaded %d %s (service hooks bypassed)",
        len(docs),
        entity_name,
    )


def apply():
    """Route the tracked collections through the right loader."""
    try:
        from apps.prepopulate import app_initialize
        from superdesk.core import get_app_config
    except Exception as exc:
        logger.error("content_config_patch: could not import core init data: %r", exc)
        return

    if getattr(app_initialize.import_file, "_pesacheck_content_config", False):
        return

    original_import_file = app_initialize.import_file

    def _base_dir(path):
        return Path(path) if path else Path(get_app_config("INIT_DATA_PATH", "."))

    async def import_file(
        entity_name, path, file_name, index_params, do_patch=False, force=False
    ):
        if entity_name == "vocabularies" and file_name == "vocabularies.json":
            vocab_dir = _base_dir(path) / "vocabularies"
            if vocab_dir.is_dir():
                docs = _load_split_vocabularies(vocab_dir)
                logger.info(
                    "content_config: loading %d vocabularies from %s",
                    len(docs),
                    vocab_dir,
                )
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "vocabularies.json").write_text(
                        json.dumps(docs), encoding="utf-8"
                    )
                    return await original_import_file(
                        entity_name, tmp, file_name, index_params, do_patch, force
                    )
            logger.warning(
                "content_config: no split vocabularies dir at %s; using core fallback",
                vocab_dir,
            )

        elif entity_name in RAW_LOAD_ENTITIES and file_name:
            file_path = _base_dir(path) / file_name
            if file_path.is_file():
                return await _raw_drop_load(
                    entity_name, file_path, index_params, original_import_file
                )
            logger.warning(
                "content_config: no tracked %s at %s; using core loader",
                entity_name,
                file_path,
            )

        return await original_import_file(
            entity_name, path, file_name, index_params, do_patch, force
        )

    import_file._pesacheck_content_config = True
    app_initialize.import_file = import_file
    logger.info(
        "content_config_patch: vocabularies split-tree loader (deny %s); raw drop-load for %s",
        sorted(VOCABULARY_DENY_LIST),
        sorted(RAW_LOAD_ENTITIES),
    )

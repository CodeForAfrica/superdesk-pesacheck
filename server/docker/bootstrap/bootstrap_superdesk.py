#!/usr/bin/env python3
"""Bootstrap the local Superdesk instance.

Runs as the `superdesk-bootstrap` compose service (profile `bootstrap`) via the
`bootstrap-superdesk.sh` entrypoint. Every phase is an idempotent
create-or-reconcile, so `make seed` serves both first-time setup and repairing a
drifted local stack.

Phases run in order and each assumes the ones before it:

  1. initialize_base_data                manage.py app:initialize_data, admin user
  2. report_known_index_conflicts        explain the IndexKeySpecsConflict
                                         traceback phase 1 emits (upstream bug)
  3. repair_generated_data               flags and desk types the generated data
                                         leaves unset
  4. restore_content_config              overlay the UAT content-config dump and
                                         repoint it at this instance
  5. seed_publisher_subscriber           product + HTTP push destination pointing
                                         at publisher-nginx
  6. seed_demo_content                   LOCAL-PUBLISHER-* smoke-test stories;
                                         skipped when SUPERDESK_DEMO_DATA=0

Environment:
  MONGO_URI, ARCHIVED_URI            Superdesk databases
  SUPERDESK_CONTENT_CONFIG_ARCHIVE   path to the content-config mongodump tgz
  SUPERDESK_FORCE_INITIALIZE_DATA    re-run app:initialize_data even if base data exists
  SUPERDESK_DEMO_DATA                set to 0 to skip phase 6
  SUPERDESK_DEMO_DESK_NAME           desk the demo stories are filed to
  SUPERDESK_INTERNAL_API_URL         API base used for the demo-content POSTs
"""

import json
import os
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId, decode_file_iter, json_util
from pymongo import ASCENDING, DESCENDING, MongoClient

DEFAULT_MONGO_URI = "mongodb://superdesk-mongodb/superdesk"
DEFAULT_ARCHIVED_URI = "mongodb://superdesk-mongodb/superdesk_archive"
DEFAULT_CONFIG_ARCHIVE = (
    "/opt/superdesk/local-content-config/superdesk-content-config.tgz"
)
DEFAULT_INTERNAL_API_URL = "http://superdesk-api:5000/api"

# Collections the content-config dump owns outright: each is dropped and
# replaced wholesale, so anything edited locally in these is not preserved.
CONFIG_COLLECTIONS = [
    "content_types",
    "content_templates",
    "vocabularies",
    "content_filters",
    "coverage_profiles",
    "planning_types",
    "desks",
    "stages",
]

# Local-only content-profile overrides, applied after the restore. The UAT
# export caps a few headline-ish fields (slugline 24, headline 64, abstract 160)
# and the PesaCheck editorial workflow needs them unbounded.
#
# Kept here rather than edited into the dump: the dump is an opaque mongodump
# re-exported from UAT periodically, so an edit there is both unreviewable in a
# diff and silently reverted on the next refresh.
UNCAPPED_PROFILE_FIELDS = {
    "Article": ["slugline", "abstract", "headline"],
}

# Local-only content-profile field additions, applied after the restore.
#
# `keywords` is where Ghost ingest puts every tag it cannot map onto a
# vocabulary (see pesacheck/tags.py), but the UAT Article profile carries
# `schema.keywords = None` and no `editor.keywords` entry at all.
#
# A None schema value makes core's `is_enabled()` false. That does NOT strip the
# field on the way in -- Mongo keeps it -- but `apply_schema()` runs inside the
# publish formatter (superdesk/publish_async/formatters/base_exchange_formatter),
# so the field is dropped from the outgoing ninjs. The leftovers were therefore
# stored and invisible: absent from the authoring view, and absent from every
# item Publisher received. Measured on the local stack 2026-08-28 before this
# override: `keywords` missing from all 235 transmitted ninjs payloads,
# Publisher's `swp_keyword` table empty. After it: the key is present and
# Publisher populates `swp_keyword` / `swp_article_keyword`.
#
# Both halves have to be written: the schema entry is what lets the field
# through the formatter, the editor entry is what renders it in authoring.
#
# `order` deliberately sits past the profile's highest (28) so this appends to
# the end of the header section instead of renumbering the UAT layout.
PROFILE_FIELD_ADDITIONS = {
    "Article": {
        "keywords": {
            "editor": {
                "order": 29,
                "sdWidth": "full",
                "section": "header",
                "enabled": True,
                "required": False,
                "readonly": False,
            },
            "schema": {"type": "list", "required": False, "nullable": True},
        },
    },
}

# Vocabulary items ingest emits that the UAT dump does not define. A subject
# entry whose qcode is absent from its vocabulary still validates and still
# reaches Publisher; it simply renders as a blank label, so this is invisible
# until someone opens the item.
#
#   Debunk       pesacheck/debunk.py maps ten ratings and the dump carries
#                seven. Measured over the full pesacheck.org corpus (2026-08-28)
#                that sends 350 items -- 2.4% of everything rated -- to
#                `true`, `misleading` or `mixture`. Added rather than remapped:
#                "TRUE" has no honest equivalent among the seven, and the
#                headline prefix is a deliberate editorial verdict.
#   content_type Ghost tags content `Short Form` / `Long Form`; the dump has
#                `Quick Read` and `Explainer`. Short Form is Quick Read, but
#                Long Form is NOT an explainer -- it is its own thing, so it
#                gets its own item (editorial call, 2026-08-28).
#
# Additive and keyed on qcode: an item already present is left exactly as the
# dump defines it, so this cannot fight a future dump that adds them properly.
VOCABULARY_ITEM_ADDITIONS = {
    "Debunk": [
        {"name": "True", "qcode": "true", "is_active": True},
        {"name": "Misleading", "qcode": "misleading", "is_active": True},
        {"name": "Mixture", "qcode": "mixture", "is_active": True},
    ],
    "content_type": [
        {"name": "Long Form", "qcode": "longform", "is_active": True},
    ],
}

# qcode corrections, as (vocabulary, item name, wrong qcode, right qcode).
# Matched on BOTH name and current qcode so a dump that fixes this upstream is
# left alone rather than being "repaired" back into a different wrong state.
#
# countrymention1 codes DR Congo as COG, which is ISO 3166 for
# Congo-Brazzaville; Kinshasa is COD. The `countries` vocabulary has both, and
# correctly -- so left alone the Primary country and Countries mentioned fields
# disagree on every DRC post (~570 in the full corpus).
VOCABULARY_QCODE_REPAIRS = [
    ("countrymention1", "DR Congo", "COG", "COD"),
]

# Mojibake signatures: UTF-8 bytes that were decoded as Latin-1 and re-encoded
# as UTF-8, so "Côte" is stored as "CÃ´te". Three items in the dump's
# `countries` vocabulary are affected. Matching is normalisation-insensitive
# (pesacheck/tags.py), so this is cosmetic for the mapping -- but the name is
# what the client and everything downstream of Publisher display.
MOJIBAKE_MARKERS = ("Ã", "Â", "â€")

AVAILABILITY_MANAGER_TAGS = {
    "_id": "availability_manager_tags",
    "display_name": "Availability Manager Tags",
    "type": "manageable",
    "unique_field": "qcode",
    "selection_type": "multi selection",
    "items": [],
    "schema": {
        "name": {"type": "string"},
        "qcode": {"type": "string"},
        "parent": {"type": "string"},
        "translations": {"type": "dict"},
    },
}

PUBLISHER_PRODUCT_NAME = "Local Publisher Product"
PUBLISHER_SUBSCRIBER_NAME = "Local Publisher Subscriber"
# The seeded HTTP-push destination for the Local Publisher subscriber. The
# Compose default targets the `publisher-nginx` service name; deployments where
# that name does not resolve (e.g. AWS ECS bridge networking) override these with
# an in-VPC address such as http://publisher.<env>.internal.
PUBLISHER_PUSH_URL = os.environ.get(
    "PUBLISHER_PUSH_URL", "http://publisher-nginx/api/v2/content/push"
)
PUBLISHER_ASSETS_URL = os.environ.get(
    "PUBLISHER_ASSETS_URL", "http://publisher-nginx/api/v2/assets/push"
)

DEMO_SLUGLINE_PREFIX = "LOCAL-PUBLISHER"
DEMO_STORIES = [
    (
        "LOCAL-PUBLISHER-1",
        "Local Publisher smoke test story",
        "<p>This is a seeded Superdesk article for the local Publisher integration.</p>",
    ),
    (
        "LOCAL-PUBLISHER-2",
        "Docker newsroom seed is ready",
        "<p>This draft exists so the monitoring view has content immediately after bootstrap.</p>",
    ),
    (
        "LOCAL-PUBLISHER-3",
        "Publisher destination can be tested locally",
        "<p>Use this item to test publishing into the local Superdesk Publisher service.</p>",
    ),
]

OBJECT_ID_HEX_RE = re.compile(r"^[0-9a-fA-F]{24}$")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def step(title):
    print(f"\n== {title}", flush=True)


def env_flag(name, default):
    return os.environ.get(name, default) == "1"


def db_from_uri(uri, default_name):
    # Strip any query string before reading the database name, or a URI like
    # mongodb://host/superdesk?authSource=admin yields "superdesk?authSource=admin".
    db_name = uri.rsplit("/", 1)[-1].split("?", 1)[0] or default_name
    return MongoClient(uri)[db_name]


def superdesk_db():
    return db_from_uri(os.environ.get("MONGO_URI", DEFAULT_MONGO_URI), "superdesk")


def utcnow():
    return datetime.now(timezone.utc)


def new_etag():
    return secrets.token_hex(20)


def require_admin(db, purpose):
    admin = db.users.find_one({"username": "admin"})
    if not admin:
        raise SystemExit(f"Cannot {purpose} because the admin user does not exist.")
    return admin


def run_manage(*args, check=True, capture=False):
    return subprocess.run(
        ["python3", "manage.py", *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def ensure_availability_manager_tags(db):
    # Upserted in two places because the content-config restore drops the whole
    # vocabularies collection; this keeps the vocabulary present either way.
    db.vocabularies.update_one(
        {"_id": AVAILABILITY_MANAGER_TAGS["_id"]},
        {"$setOnInsert": AVAILABILITY_MANAGER_TAGS},
        upsert=True,
    )


# --------------------------------------------------------------------------
# 1. Base data
# --------------------------------------------------------------------------


def has_base_data(db):
    has_admin = db.users.count_documents({"username": "admin"}) > 0
    has_config = (
        db.validators.estimated_document_count() > 0
        and db.config.estimated_document_count() > 0
    )
    return has_admin and has_config


def initialize_base_data():
    step("Initializing base data")
    if has_base_data(superdesk_db()) and not env_flag(
        "SUPERDESK_FORCE_INITIALIZE_DATA", "0"
    ):
        print(
            "Skipping app:initialize_data because base Superdesk data already exists."
        )
    else:
        run_manage("app:initialize_data")

    # Fails loudly on a re-run when admin already exists; that is expected.
    run_manage(
        "users:create",
        "-u",
        "admin",
        "-p",
        "admin",
        "-e",
        "admin@localhost",
        "--admin",
        check=False,
    )


# --------------------------------------------------------------------------
# 2. Known index conflicts
# --------------------------------------------------------------------------

# Superdesk defines a handful of Mongo indexes twice, with conflicting key
# specs, and the two definitions fight inside `app:initialize_data`:
#
#   superdesk/archived_async/module.py:26
#       keys=[("item_id", 1), ("background", 1)]   <- `background` is an index
#   apps/prepopulate/app_initialize.py:154            *option*, not a document
#       [("item_id", pymongo.ASCENDING)]              field; it does not belong
#                                                     in a key spec
#
# The async registry runs first and, on a spec conflict, drops the existing
# index and recreates it with its own malformed spec
# (superdesk/core/mongo/mongo_manager.py:208-216). The prepopulate pass then
# asks for the correct spec, gets IndexKeySpecsConflict (code 86), and logs a
# traceback ending in "Exception loading entity archived".
#
# Noisy but harmless: no document has a `background` field, so the index is
# effectively just `item_id` with a dead trailing component and queries on
# item_id still use it. The `archived` entity has no data file to import, so the
# aborted handler skips nothing but the index it could not create.
#
# There is deliberately no attempt to repair this. Dropping the index first does
# not work -- the async registry recreates it during the same run, which is why
# the bootstrap used to carry a pre-flight drop that never actually helped. The
# fix belongs upstream in superdesk-core. This reports the state instead, so the
# traceback is not the last word on it.
INDEX_OPTION_FIELDS = {"background", "sparse", "unique", "expireAfterSeconds"}


def malformed_indexes(db):
    """Yield (collection, index) pairs whose key spec contains an index option."""
    for collection_name in db.list_collection_names():
        for index in db[collection_name].list_indexes():
            leaked = INDEX_OPTION_FIELDS.intersection(index.get("key", {}))
            if leaked:
                yield collection_name, index


def report_known_index_conflicts():
    """Explain the IndexKeySpecsConflict traceback app:initialize_data emits."""
    step("Checking for known index conflicts")
    targets = [
        (os.environ.get("MONGO_URI", DEFAULT_MONGO_URI), "superdesk"),
        (os.environ.get("ARCHIVED_URI", DEFAULT_ARCHIVED_URI), "superdesk_archive"),
    ]
    found = []
    for uri, default_name in targets:
        db = db_from_uri(uri, default_name)
        for collection_name, index in malformed_indexes(db):
            found.append(
                f"{db.name}.{collection_name}.{index['name']} {dict(index['key'])}"
            )

    if not found:
        print("None.")
        return

    print(
        "Known upstream defect: these indexes carry an index option inside their\n"
        "key spec, because superdesk-core declares them twice with conflicting\n"
        "specs. Any IndexKeySpecsConflict traceback above is this, and is safe to\n"
        "ignore -- see the comment above report_known_index_conflicts()."
    )
    for entry in found:
        print(f"  {entry}")


# --------------------------------------------------------------------------
# 3. Repair generated data
# --------------------------------------------------------------------------


def repair_generated_data():
    """Fix up what app:initialize_data leaves in a state the UI rejects."""
    step("Repairing generated data")
    db = superdesk_db()
    admin = require_admin(db, "finish bootstrap")
    now = utcnow()

    ensure_availability_manager_tags(db)

    db.users.update_one(
        {"_id": admin["_id"]},
        {
            "$set": {
                "user_type": "administrator",
                "is_active": True,
                "is_enabled": True,
                "is_author": True,
                "_updated": now,
            }
        },
    )

    desks = db.desks.update_many(
        {}, {"$set": {"desk_type": "production", "_updated": now}}
    )
    if desks.modified_count:
        print(f"Marked {desks.modified_count} bootstrap desk(s) as production desks.")

    # A null `renditions` on a media item crashes the monitoring view.
    media = db.archive.update_many(
        {"type": {"$in": ["picture", "audio", "video"]}, "renditions": None},
        {"$set": {"renditions": {}, "_updated": now}},
    )
    if media.modified_count:
        print(
            f"Repaired {media.modified_count} media archive items with null renditions."
        )


# --------------------------------------------------------------------------
# 4. Content config restore
# --------------------------------------------------------------------------


def profile_slug(profile, fallback):
    label = str(profile.get("label") or profile.get("type") or fallback)
    slug = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
    return slug or fallback


def restore_collection(db, collection_name, source_dir):
    """Replace one collection with the dump's copy, including its indexes."""
    bson_path = source_dir / f"{collection_name}.bson"
    if not bson_path.exists():
        db[collection_name].drop()
        print(f"Dropped {collection_name}; not present in archive.")
        return

    with bson_path.open("rb") as handle:
        documents = list(decode_file_iter(handle))
    db[collection_name].drop()
    if documents:
        db[collection_name].insert_many(documents)

    metadata_path = source_dir / f"{collection_name}.metadata.json"
    if metadata_path.exists():
        metadata = json_util.loads(metadata_path.read_text())
        for index in metadata.get("indexes", []):
            if index.get("name") == "_id_":
                continue
            keys = [
                (key, ASCENDING if int(direction) >= 0 else DESCENDING)
                for key, direction in index.get("key", {}).items()
            ]
            options = {k: v for k, v in index.items() if k not in {"v", "key", "ns"}}
            db[collection_name].create_index(keys, **options)

    print(f"Restored {collection_name}: {len(documents)} documents.")


def slugify_profile_ids(db):
    """Rewrite ObjectId-shaped content profile `_id`s to readable slugs.

    Superdesk's own profiles use slugs ("text", "picture"); the UAT export
    carries ObjectIds. Returns {old_id: new_id} so references can be repointed.
    """
    converted = {}
    for profile in db.content_types.find({}):
        current_id = profile.get("_id")
        is_object_id_shaped = isinstance(current_id, ObjectId) or (
            isinstance(current_id, str) and OBJECT_ID_HEX_RE.match(current_id)
        )
        if not is_object_id_shaped:
            continue

        new_id = profile_slug(profile, f"profile_{current_id}")
        if new_id != current_id and db.content_types.count_documents({"_id": new_id}):
            new_id = f"{new_id}_{current_id}"

        profile["_id"] = new_id
        db.content_types.delete_one({"_id": current_id})
        db.content_types.insert_one(profile)
        converted[current_id] = new_id
    return converted


def repoint_profile_references(db, converted_profile_ids):
    for old_id, new_id in converted_profile_ids.items():
        # The same id can be stored as ObjectId, its str(), or its raw hex.
        candidates = [old_id, str(old_id)]
        if isinstance(old_id, ObjectId):
            candidates.append(old_id.binary.hex())

        db.desks.update_many(
            {"default_content_profile": {"$in": candidates}},
            {"$set": {"default_content_profile": new_id}},
        )
        db.content_templates.update_many(
            {"data.profile": {"$in": candidates}},
            {"$set": {"data.profile": new_id}},
        )
        db.archive.update_many(
            {"profile": {"$in": candidates}},
            {"$set": {"profile": new_id}},
        )


def reassign_ownership(db, admin, now):
    """Point the restored config at this instance's admin user."""
    if not db.desks.find_one(sort=[("name", ASCENDING)]):
        return
    db.content_types.update_many(
        {},
        {
            "$set": {
                "created_by": admin["_id"],
                "updated_by": admin["_id"],
                "_updated": now,
                "_etag": new_etag(),
            }
        },
    )
    db.desks.update_many(
        {},
        {
            "$set": {
                "desk_type": "production",
                "members": [{"user": admin["_id"]}],
                "_updated": now,
                "_etag": new_etag(),
            }
        },
    )
    db.content_templates.update_many(
        {"user": {"$exists": True}},
        {"$set": {"user": admin["_id"], "_updated": now, "_etag": new_etag()}},
    )


def remove_profile_maxlengths(db, now):
    """Apply UNCAPPED_PROFILE_FIELDS to the restored profiles.

    $unset rather than maxlength: None -- an uncapped field in this schema
    simply has no maxlength key, so this leaves the profile in the exact shape
    Superdesk writes when the limit is cleared in the UI.
    """
    for profile_label, field_names in UNCAPPED_PROFILE_FIELDS.items():
        profile = db.content_types.find_one({"label": profile_label})
        if not profile:
            print(
                f"No content profile labelled {profile_label!r}; skipping maxlength removal."
            )
            continue

        schema = profile.get("schema") or {}
        capped = [
            f for f in field_names if (schema.get(f) or {}).get("maxlength") is not None
        ]
        if not capped:
            print(f"{profile_label}: no maxlength to remove.")
            continue

        db.content_types.update_one(
            {"_id": profile["_id"]},
            {
                "$unset": {f"schema.{field}.maxlength": "" for field in capped},
                "$set": {"_updated": now, "_etag": new_etag()},
            },
        )
        print(f"Removed maxlength from {profile_label}: {', '.join(capped)}.")


def add_profile_fields(db, now):
    """Apply PROFILE_FIELD_ADDITIONS to the restored profiles.

    Writes the editor entry and the schema entry together; either alone is
    useless. See the constant's comment for why both are needed.
    """
    for profile_label, fields in PROFILE_FIELD_ADDITIONS.items():
        profile = db.content_types.find_one({"label": profile_label})
        if not profile:
            print(
                f"No content profile labelled {profile_label!r}; skipping field additions."
            )
            continue

        editor = profile.get("editor") or {}
        schema = profile.get("schema") or {}
        updates = {}
        added = []
        for field, spec in fields.items():
            # Only write what is missing: a dump that already enables the field
            # (or an editor who has since positioned it) wins over this default.
            halves = []
            if not editor.get(field):
                updates[f"editor.{field}"] = spec["editor"]
                halves.append("editor")
            if not schema.get(field):
                updates[f"schema.{field}"] = spec["schema"]
                halves.append("schema")
            if halves:
                added.append(f"{field} ({'+'.join(halves)})")

        if not updates:
            print(f"{profile_label}: no fields to add.")
            continue

        updates["_updated"] = now
        updates["_etag"] = new_etag()
        db.content_types.update_one({"_id": profile["_id"]}, {"$set": updates})
        print(f"Enabled on {profile_label}: {', '.join(added)}.")


def add_vocabulary_items(db, now):
    """Append VOCABULARY_ITEM_ADDITIONS to the restored vocabularies."""
    for vocabulary_id, additions in VOCABULARY_ITEM_ADDITIONS.items():
        vocabulary = db.vocabularies.find_one({"_id": vocabulary_id})
        if not vocabulary:
            print(f"No vocabulary {vocabulary_id!r}; skipping item additions.")
            continue

        items = list(vocabulary.get("items") or [])
        present = {item.get("qcode") for item in items}
        missing = [item for item in additions if item["qcode"] not in present]
        if not missing:
            print(f"{vocabulary_id}: all items already present.")
            continue

        db.vocabularies.update_one(
            {"_id": vocabulary_id},
            {
                "$set": {
                    "items": items + missing,
                    "_updated": now,
                    "_etag": new_etag(),
                }
            },
        )
        print(
            f"Added to {vocabulary_id}: "
            + ", ".join(f"{i['name']} ({i['qcode']})" for i in missing)
            + "."
        )


def repair_vocabulary_qcodes(db, now):
    """Apply VOCABULARY_QCODE_REPAIRS to the restored vocabularies."""
    for vocabulary_id, name, wrong_qcode, right_qcode in VOCABULARY_QCODE_REPAIRS:
        vocabulary = db.vocabularies.find_one({"_id": vocabulary_id})
        if not vocabulary:
            print(f"No vocabulary {vocabulary_id!r}; skipping qcode repair.")
            continue

        items = list(vocabulary.get("items") or [])
        targets = [
            item
            for item in items
            if item.get("name") == name and item.get("qcode") == wrong_qcode
        ]
        if not targets:
            print(
                f"{vocabulary_id}: {name} is not coded {wrong_qcode}; nothing to repair."
            )
            continue

        for item in targets:
            item["qcode"] = right_qcode
        db.vocabularies.update_one(
            {"_id": vocabulary_id},
            {"$set": {"items": items, "_updated": now, "_etag": new_etag()}},
        )
        print(f"Repaired {vocabulary_id}: {name} {wrong_qcode} -> {right_qcode}.")


def demojibake(text):
    """Undo one round of UTF-8-decoded-as-Latin-1, or return ``text`` unchanged.

    Only attempted when a marker byte sequence is present, so a name that
    legitimately contains "Ã" is not mangled, and only accepted when the
    round-trip actually succeeds.
    """
    if not isinstance(text, str) or not any(m in text for m in MOJIBAKE_MARKERS):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def repair_vocabulary_mojibake(db, now):
    """Re-decode double-encoded item names across every restored vocabulary.

    Swept over all vocabularies rather than a named list: the defect is a
    property of how the dump was exported, so the next refresh can just as
    easily land it somewhere else.
    """
    repaired_vocabularies = 0
    repaired_names = []
    for vocabulary in db.vocabularies.find({"items": {"$exists": True}}):
        items = list(vocabulary.get("items") or [])
        changed = False
        for item in items:
            fixed = demojibake(item.get("name"))
            if fixed != item.get("name"):
                repaired_names.append(f"{vocabulary['_id']}.{item['qcode']} -> {fixed}")
                item["name"] = fixed
                changed = True
        if not changed:
            continue
        db.vocabularies.update_one(
            {"_id": vocabulary["_id"]},
            {"$set": {"items": items, "_updated": now, "_etag": new_etag()}},
        )
        repaired_vocabularies += 1

    if not repaired_names:
        print("No mojibake in vocabulary item names.")
        return
    print(
        f"Repaired {len(repaired_names)} mojibake item name(s) "
        f"across {repaired_vocabularies} vocabular(ies):"
    )
    for entry in repaired_names:
        print(f"  {entry}")


def restore_content_config():
    """Overlay the UAT content-config dump, then repoint it at this instance.

    No-op (not an error) when the archive is absent: the generated defaults from
    app:initialize_data are a usable, if bare, newsroom.
    """
    step("Restoring content config")
    archive_path = Path(
        os.environ.get("SUPERDESK_CONTENT_CONFIG_ARCHIVE", DEFAULT_CONFIG_ARCHIVE)
    )
    if not archive_path.exists():
        print(
            f"No content config archive found at {archive_path}; keeping generated defaults."
        )
        return

    db = superdesk_db()
    admin = require_admin(db, "restore content config")
    now = utcnow()

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(archive_path) as archive:
            archive.extractall(tmpdir, filter="data")

        bson_files = list(Path(tmpdir).rglob("*.bson"))
        if not bson_files:
            raise SystemExit(f"No BSON files found in {archive_path}.")
        source_dir = bson_files[0].parent

        print(f"Restoring content config from {archive_path}.")
        for collection_name in CONFIG_COLLECTIONS:
            restore_collection(db, collection_name, source_dir)

    repoint_profile_references(db, slugify_profile_ids(db))
    reassign_ownership(db, admin, now)
    remove_profile_maxlengths(db, now)
    add_profile_fields(db, now)
    add_vocabulary_items(db, now)
    repair_vocabulary_qcodes(db, now)
    repair_vocabulary_mojibake(db, now)
    ensure_availability_manager_tags(db)

    print("Content config restore and local reference repair complete.")


# --------------------------------------------------------------------------
# 5. Publisher subscriber
# --------------------------------------------------------------------------


def upsert_by_name(collection, name, fields, now):
    """Create-or-update keyed on `name`, preserving the existing `_id`."""
    existing = collection.find_one({"name": name})
    doc_id = existing["_id"] if existing else ObjectId()
    collection.update_one(
        {"_id": doc_id},
        {
            "$set": {"name": name, "_updated": now, **fields},
            "$setOnInsert": {"_id": doc_id, "_created": now},
        },
        upsert=True,
    )
    return doc_id


def seed_publisher_subscriber():
    """Wire Superdesk's publishing pipeline at the local Publisher.

    Delivery is container-to-container, so the destination uses the
    publisher-nginx service name, not the host-published port.
    """
    step("Seeding Publisher subscriber")
    db = superdesk_db()
    now = utcnow()

    product_id = upsert_by_name(
        db.products,
        PUBLISHER_PRODUCT_NAME,
        {
            "description": "Routes locally published Superdesk items to Superdesk Publisher.",
            "codes": "local-publisher",
            "content_filter": None,
            "geo_restrictions": None,
            "product_type": "direct",
        },
        now,
    )

    upsert_by_name(
        db.subscribers,
        PUBLISHER_SUBSCRIBER_NAME,
        {
            "subscriber_type": "wire",
            "media_type": "media",
            "email": "publisher@localhost",
            "is_active": True,
            "is_targetable": True,
            "sequence_num_settings": {"min": 1, "max": 999999},
            "critical_errors": {"9004": True},
            "destinations": [
                {
                    "_id": "local-publisher-http-push",
                    "name": "Local Publisher HTTP Push",
                    "delivery_type": "http_push",
                    "format": "ninjs",
                    "config": {
                        "resource_url": PUBLISHER_PUSH_URL,
                        "assets_url": PUBLISHER_ASSETS_URL,
                        "packaged": False,
                    },
                }
            ],
            "products": [product_id],
            "api_products": [],
            "global_filters": {},
            "async": False,
            "priority": False,
        },
        now,
    )

    print("Seeded Local Publisher Product and HTTP Push subscriber.")


# --------------------------------------------------------------------------
# 6. Demo content
# --------------------------------------------------------------------------


def wait_for_api(api_base, attempts=30, delay=2):
    for attempt in range(attempts):
        try:
            urllib.request.urlopen(f"{api_base}/", timeout=2).read()
            return
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def admin_auth_token():
    result = run_manage(
        "users:get_auth_token", "-u", "admin", "-p", "admin", capture=True
    )
    output = result.stdout + result.stderr
    match = re.search(r"Generated token:\s+b'([^']+)'", output)
    if not match:
        raise SystemExit(
            f"Could not parse admin auth token from users:get_auth_token output:\n{output}"
        )
    return match.group(1)


def api_post(api_base, auth_token, resource, payload):
    request = urllib.request.Request(
        f"{api_base}/{resource}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": auth_token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"POST {resource} failed with {error.code}: {body}"
        ) from error


def resolve_demo_desk(db, admin, now, post):
    desk = (
        db.desks.find_one(
            {"name": os.environ.get("SUPERDESK_DEMO_DESK_NAME", "Newsdesk")}
        )
        or db.desks.find_one({"default_content_profile": {"$exists": True}})
        or db.desks.find_one()
    )
    if not desk:
        return post(
            "desks",
            {
                "name": "Local News Desk",
                "desk_type": "production",
                "members": [{"user": str(admin["_id"])}],
            },
        )

    db.desks.update_one(
        {"_id": desk["_id"]},
        {"$set": {"desk_type": "production", "_updated": now}},
    )
    return desk


def seed_demo_content():
    """Create (or repair) the LOCAL-PUBLISHER-* smoke-test stories.

    Goes through the REST API rather than Mongo so the items get the same
    validation, Elastic indexing, and generated fields as real content.
    """
    step("Seeding demo content")
    db = superdesk_db()
    admin = require_admin(db, "seed demo content")
    now = utcnow()

    api_base = os.environ.get(
        "SUPERDESK_INTERNAL_API_URL", DEFAULT_INTERNAL_API_URL
    ).rstrip("/")
    wait_for_api(api_base)
    auth_token = admin_auth_token()

    def post(resource, payload):
        return api_post(api_base, auth_token, resource, payload)

    desk = resolve_demo_desk(db, admin, now, post)
    desk_name = desk.get("name", "local desk")

    demo_defaults = {
        "type": "text",
        "profile": str(desk.get("default_content_profile") or "text"),
        "task": {
            "desk": str(desk["_id"]),
            "stage": str(desk.get("incoming_stage") or desk.get("working_stage")),
            "user": str(admin["_id"]),
        },
    }
    if desk.get("default_content_template"):
        demo_defaults["template"] = str(desk["default_content_template"])

    # Re-point any stories from a previous seed: the restore above may have
    # changed the desk, stage, or profile they referenced.
    existing = {"slugline": {"$regex": f"^{DEMO_SLUGLINE_PREFIX}"}}
    existing_count = db.archive.count_documents(existing)
    if existing_count:
        db.archive.update_many(existing, {"$set": demo_defaults})
    if existing_count >= len(DEMO_STORIES):
        print(f"Repaired existing local Publisher demo stories for {desk_name}.")
        return

    for slugline, headline, body_html in DEMO_STORIES:
        if db.archive.count_documents({"slugline": slugline}):
            continue
        post(
            "archive",
            {
                "headline": headline,
                "slugline": slugline,
                "body_html": body_html,
                "state": "in_progress",
                **demo_defaults,
            },
        )

    print(f"Seeded {desk_name} and local Publisher demo stories.")


# --------------------------------------------------------------------------


def main():
    initialize_base_data()
    report_known_index_conflicts()
    repair_generated_data()
    restore_content_config()
    seed_publisher_subscriber()

    if env_flag("SUPERDESK_DEMO_DATA", "1"):
        seed_demo_content()
    else:
        step("Skipping demo content (SUPERDESK_DEMO_DATA=0)")

    print("\nBootstrap complete.")


if __name__ == "__main__":
    main()

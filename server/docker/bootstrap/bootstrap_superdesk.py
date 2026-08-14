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

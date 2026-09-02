#!/usr/bin/env python3
"""Bootstrap the local Superdesk instance.

Runs as the `superdesk-bootstrap` compose service (profile `bootstrap`) via the
`bootstrap-superdesk.sh` entrypoint. Every phase is an idempotent
create-or-reconcile, so `make seed` serves both first-time setup and repairing a
drifted local stack.

Phases run in order and each assumes the ones before it:

  1. initialize_base_data                manage.py app:initialize_data --force,
                                         admin user. Loads the tracked content
                                         config from data/ (vocabularies via the
                                         split-tree loader, content_config_patch)
  2. report_known_index_conflicts        explain the IndexKeySpecsConflict
                                         traceback phase 1 emits (upstream bug)
  3. repair_generated_data               flags and desk types the generated data
                                         leaves unset
  4. reassign_content_ownership          point profiles/desks/stages at this
                                         instance's admin (per-instance, stays code)
  5. seed_publisher_subscriber           product + HTTP push destination pointing
                                         at publisher-nginx
  6. seed_demo_content                   LOCAL-PUBLISHER-* smoke-test stories;
                                         skipped when SUPERDESK_DEMO_DATA=0

Environment:
  MONGO_URI, ARCHIVED_URI            Superdesk databases
  SUPERDESK_DEMO_DATA                set to 0 to skip phase 6
  SUPERDESK_DEMO_DESK_NAME           desk the demo stories are filed to
  SUPERDESK_INTERNAL_API_URL         API base used for the demo-content POSTs
"""

import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import ASCENDING, MongoClient

DEFAULT_MONGO_URI = "mongodb://superdesk-mongodb/superdesk"
DEFAULT_ARCHIVED_URI = "mongodb://superdesk-mongodb/superdesk_archive"
DEFAULT_INTERNAL_API_URL = "http://superdesk-api:5000/api"


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


# --------------------------------------------------------------------------
# 1. Base data
# --------------------------------------------------------------------------


def initialize_base_data():
    step("Initializing base data")
    # Always run, and always force. The tracked JSON under data/ is now the source
    # of truth for the content config (loaded here via
    # pesacheck/content_config_patch for the split vocabularies tree). Core skips
    # app:initialize_data on a populated database, and even when it runs it updates
    # a document only when forced or the file's init_version is newer -- so an
    # unforced reseed would silently apply nothing at all. Forcing unconditionally
    # is what makes an edit to a tracked file actually take on reseed.
    run_manage("app:initialize_data", "--force")

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
# 4. Content ownership
# --------------------------------------------------------------------------


def reassign_ownership(db, admin, now):
    """Point the tracked content config at this instance's admin user."""
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
    refresh_stage_visibility(db, now)


def refresh_stage_visibility(db, now):
    """Recompute each user's cached ``invisible_stages`` from desk membership.

    Superdesk caches, on every user, the ids of stages that user may NOT see:
    the hidden (``is_visible: false``) stages of desks the user is *not* a member
    of. Search and the desk Output view read this cache verbatim
    (apps/search `SearchService.get_stages_to_exclude`), so a stale value silently
    hides published content. The authoritative computation is core's
    ``apps/stages.py`` ``get_stages_by_visibility(is_visible=False, user_desk_ids)``
    -> ``{"is_visible": False, "desk": {"$nin": user_desk_ids}}``; core keeps the
    cache fresh via ``update_stage_visibility_for_users`` on every stage-visibility
    or membership change *made through the service*.

    Both the stage load (raw drop-load from the tracked JSON, see
    pesacheck/content_config_patch) and the membership assignment above are raw
    Mongo writes that bypass those service hooks, so nothing recomputes the cache.
    Worse, the hidden stages now exist *before* ``users:create`` runs (they load
    inside ``app:initialize_data --force``), so the admin's ``on_created`` hook
    caches every hidden stage as invisible while it is not yet a desk member --
    and no later step clears it. The result: auto-published fact-checks live on the
    hidden ``Pitches`` incoming stage and vanish from /search and Output even though
    they publish fine and reach Publisher. This restores exact-parity with the old
    tgz-restore flow, where the same raw writes happened to leave the cache empty
    because the hidden stages did not exist at user-create time.

    Replicating the core query in raw Mongo rather than calling the service keeps
    this in the bootstrap's raw idiom; if core changes the visibility algorithm,
    the docstring above names the function to re-validate against.
    """
    for user in db.users.find({}, {"_id": 1}):
        user_desk_ids = [
            desk["_id"]
            for desk in db.desks.find({"members.user": user["_id"]}, {"_id": 1})
        ]
        invisible = [
            str(stage["_id"])
            for stage in db.stages.find(
                {"is_visible": False, "desk": {"$nin": user_desk_ids}}, {"_id": 1}
            )
        ]
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"invisible_stages": invisible, "_updated": now}},
        )


def reassign_content_ownership():
    """Point the tracked content config at this instance's admin user.

    The content config itself now loads from tracked JSON under data/ via
    app:initialize_data (see initialize_base_data and
    pesacheck/content_config_patch for the split vocabularies tree). The only part
    of the old tgz-restore that is not derivable from a tracked file is ownership:
    profiles, desks and stages must point at THIS newsroom's admin user, and desks
    need their membership and etags set. That is what reassign_ownership does, and
    it deliberately stays code because the admin ObjectId is per-instance.
    """
    step("Reassigning content ownership")
    db = superdesk_db()
    admin = require_admin(db, "reassign content ownership")
    reassign_ownership(db, admin, utcnow())
    print("Content ownership reassigned.")


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
    reassign_content_ownership()
    seed_publisher_subscriber()

    if env_flag("SUPERDESK_DEMO_DATA", "1"):
        seed_demo_content()
    else:
        step("Skipping demo content (SUPERDESK_DEMO_DATA=0)")

    print("\nBootstrap complete.")


if __name__ == "__main__":
    main()

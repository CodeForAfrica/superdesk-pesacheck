#!/usr/bin/env bash
#
# Bootstrap (idempotent create-or-update) the Ghost CMS ingest provider.
#
# Matches the other ops/local bootstrap scripts: runs inside the superdesk
# server image and talks to Mongo with pymongo via MONGO_URI (default points
# at the in-container `superdesk-mongodb` host). The provider is keyed by its
# unique name, so re-running just reconciles the config-bearing fields — it
# never duplicates the provider and never clobbers runtime bookkeeping
# (ingested_count, last_updated, ...).
#
# Run it either as a one-off in the running worker container:
#   docker compose -f docker-compose.ecs-local.yml exec superdesk-worker \
#     bash /usr/local/bin/bootstrap-ghost-ingest.sh
# (mount it there, as the other bootstrap scripts are mounted), or wire a
# `superdesk-ghost-bootstrap` service under the `bootstrap` profile.
#
# Settings (all overridable via env vars):
#   PROVIDER_NAME    provider + source name            (default: ghost)
#   SOURCE           item "source" field               (default: Ghost)
#   INGEST_PATH      folder the feed reads, IN-CONTAINER path
#                                                       (default: /opt/superdesk/ingest/ghost)
#   GHOST_URL        Ghost site URL; resolves the __GHOST_URL__ placeholder in
#                    exported image/body links so images can be fetched
#                                                       (default: empty -> images with
#                                                        unresolved URLs are skipped)
#   CONTENT_EXPIRY   ingest TTL in MINUTES; must exceed the age of the oldest
#                    post you want, or old posts are filtered as "expired"
#                                                       (default: 5256000 = ~10 years,
#                                                        suitable for archive backfill)
#   CONTENT_TYPES    comma-separated allowed item types (default: text,picture)
#   UPDATE_MINUTES   run schedule, minutes              (default: 5)
#   IS_CLOSED        start closed? true|false           (default: false)
#   AUTO_PUBLISH     fetch + publish each ingested item automatically so it
#                    reaches Publisher without a manual step  true|false
#                                                       (default: true)
#   PUBLISH_DESK     desk the items are fetched onto before publishing; the
#                    Publisher route is chosen by language, not the desk
#                                                       (default: Newsdesk)
#   MONGO_URI        mongo connection string
#                                                       (default: mongodb://superdesk-mongodb/superdesk)
#
set -euo pipefail

export PROVIDER_NAME="${PROVIDER_NAME:-ghost}"
export SOURCE="${SOURCE:-Ghost}"
export INGEST_PATH="${INGEST_PATH:-/opt/superdesk/ingest/ghost}"
export GHOST_URL="${GHOST_URL:-https://pesacheck.org}"
export CONTENT_EXPIRY="${CONTENT_EXPIRY:-5256000}"
export CONTENT_TYPES="${CONTENT_TYPES:-text,picture}"
export UPDATE_MINUTES="${UPDATE_MINUTES:-5}"
export IS_CLOSED="${IS_CLOSED:-false}"
export AUTO_PUBLISH="${AUTO_PUBLISH:-true}"
export PUBLISH_DESK="${PUBLISH_DESK:-Newsdesk}"

python3 - <<'PY'
import os
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import MongoClient

# Feeding service + parser are fixed for Ghost — these are the registry NAMEs
# declared by GhostFeedingService / GhostParser in server/pesacheck/ingest/.
FEEDING_SERVICE = "ghost_file"
FEED_PARSER = "ghost"

mongo_uri = os.environ.get("MONGO_URI", "mongodb://superdesk-mongodb/superdesk")
db_name = mongo_uri.rsplit("/", 1)[-1] or "superdesk"
db = MongoClient(mongo_uri)[db_name]
now = datetime.now(timezone.utc)

name = os.environ["PROVIDER_NAME"]
content_types = [t.strip() for t in os.environ["CONTENT_TYPES"].split(",") if t.strip()]
ghost_url = os.environ["GHOST_URL"].strip()

set_fields = {
    "source": os.environ["SOURCE"],
    "feeding_service": FEEDING_SERVICE,
    "feed_parser": FEED_PARSER,
    "content_types": content_types,
    "content_expiry": int(os.environ["CONTENT_EXPIRY"]),
    "is_closed": os.environ["IS_CLOSED"].lower() == "true",
    "config.path": os.environ["INGEST_PATH"],
    # Read by GhostFeedingService to fetch+publish each ingested item so it
    # reaches Publisher unattended, and to pick the desk it is fetched onto.
    "config.auto_publish": os.environ["AUTO_PUBLISH"].lower() == "true",
    "config.publish_desk": os.environ["PUBLISH_DESK"],
    "update_schedule.minutes": int(os.environ["UPDATE_MINUTES"]),
    "_updated": now,
}
# Only pin config.url when a non-empty value was given, so re-running without
# GHOST_URL doesn't wipe a URL set earlier via the UI.
if ghost_url:
    set_fields["config.url"] = ghost_url

existing = db.ingest_providers.find_one({"name": name})
provider_id = existing["_id"] if existing else ObjectId()

db.ingest_providers.update_one(
    {"name": name},
    {
        "$set": set_fields,
        "$setOnInsert": {
            "_id": provider_id,
            "_created": now,
            "name": name,
            "allow_remove_ingested": False,
            "disable_item_updates": False,
            "notifications": {
                "on_update": True,
                "on_close": True,
                "on_open": True,
                "on_error": True,
            },
        },
    },
    upsert=True,
)

doc = db.ingest_providers.find_one(
    {"name": name},
    {
        "name": 1, "source": 1, "feeding_service": 1, "feed_parser": 1,
        "content_types": 1, "content_expiry": 1, "is_closed": 1,
        "config": 1, "update_schedule": 1,
    },
)
print(("Created" if existing is None else "Updated") + f" ingest provider '{name}':")
for k in ("feeding_service", "feed_parser", "content_types", "content_expiry",
          "config", "update_schedule", "is_closed"):
    print(f"  {k}: {doc.get(k)}")
PY

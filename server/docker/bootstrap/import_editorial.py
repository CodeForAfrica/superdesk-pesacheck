#!/usr/bin/env python3
"""Import the tracked editorial-page fixtures into Superdesk and publish them.

The curated Publisher content lists point at Superdesk-authored pages
(About/FAQ/Team/Ecosystem/Media-Centre/...) that a content reset does not
recreate. They are tracked as fixtures under ``server/data/editorial/`` (see
``server/scripts/editorial/`` and the plan
docs/plans/publisher-curated-list-membership.md, tier 3). This importer
re-authors + publishes them so they return to Publisher with their ORIGINAL
guids, which the Publisher membership seeder (``swp:config:seed-list-items``)
then resolves to fill the curated lists.

Goes through the internal REST API (like ``bootstrap_superdesk.py``'s demo
content), so items get real validation, generated fields, Elastic indexing and
the normal publish → transmit pipeline. The REST ``/archive`` POST honours a
provided ``guid``, so the fixtures keep their stable guids.

Idempotent: a page whose guid already exists is skipped (never re-created, never
re-published). Pages are published onto a dedicated "Static Pages" desk.

Media: each page's ``feature_media`` (hero image) and ``embedded_media`` (images
embedded in body_html, keyed by editor block id) are uploaded and attached as
associations, with the media bytes tracked as git blobs under ``data/editorial/media/``.
SWP rewrites each body ``<img src>`` to a public URL from the matching
association, so attaching them is what makes inline images render.

Env:
  MONGO_URI                     Superdesk mongo (default superdesk-mongodb)
  SUPERDESK_INTERNAL_API_URL    API base (default http://superdesk-api:5000/api)
  EDITORIAL_DATA_DIR            fixtures dir (default /opt/superdesk/data/editorial)
  STATIC_PAGES_DESK             desk name (default "Static Pages")
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import requests
from pymongo import MongoClient

DEFAULT_MONGO_URI = "mongodb://superdesk-mongodb/superdesk"
DEFAULT_API = "http://superdesk-api:5000/api"
DEFAULT_DATA = "/opt/superdesk/data/editorial"
DESK_NAME = os.environ.get("STATIC_PAGES_DESK", "Static Pages")
MEDIA_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "svg")
MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml"}


def superdesk_db():
    uri = os.environ.get("MONGO_URI", DEFAULT_MONGO_URI)
    name = uri.rsplit("/", 1)[-1].split("?")[0] or "superdesk"
    return MongoClient(uri)[name]


def wait_for_api(base, attempts=30, delay=2):
    for i in range(attempts):
        try:
            urllib.request.urlopen(f"{base}/", timeout=2).read()
            return
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def admin_token():
    out = subprocess.run(
        ["python3", "manage.py", "users:get_auth_token", "-u", "admin", "-p", "admin"],
        capture_output=True, text=True,
    )
    m = re.search(r"Generated token:\s+b'([^']+)'", out.stdout + out.stderr)
    if not m:
        raise SystemExit(f"could not parse auth token:\n{out.stdout}\n{out.stderr}")
    return m.group(1)


class Api:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = token

    def get(self, res):
        r = self.s.get(f"{self.base}/{res}", timeout=60)
        return r.status_code, _json(r)

    def post(self, res, payload):
        r = self.s.post(f"{self.base}/{res}", json=payload, timeout=90)
        return r.status_code, _json(r)

    def patch(self, res, payload, etag):
        r = self.s.patch(f"{self.base}/{res}", json=payload,
                         headers={"If-Match": etag}, timeout=120)
        return r.status_code, _json(r)

    def upload_picture(self, path, headline):
        """POST the image as multipart; ArchiveMediaService creates a picture
        item with renditions. Returns the picture doc, or None on failure."""
        mime = MIME_BY_EXT.get(path.suffix.lstrip(".").lower(), "image/jpeg")
        with path.open("rb") as fh:
            r = self.s.post(f"{self.base}/archive",
                            files={"media": (path.name, fh, mime)},
                            data={"type": "picture", "headline": headline or ""},
                            timeout=120)
        return _json(r) if r.status_code in (200, 201) else None


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def find_media(media_dir, guid):
    for ext in MEDIA_EXTS:
        p = Path(media_dir, f"{guid}.{ext}")
        if p.exists():
            return p
    return None


def ensure_desk(db, api, admin_id):
    """Return (desk_id, stage_id) for the Static Pages desk, creating it via the
    service (so stage-visibility caches are maintained by core)."""
    desk = db.desks.find_one({"name": DESK_NAME})
    if not desk:
        status, created = api.post("desks", {
            "name": DESK_NAME,
            "desk_type": "production",
            "members": [{"user": str(admin_id)}],
        })
        if status not in (200, 201):
            raise SystemExit(f"could not create desk {DESK_NAME!r}: {status} {created}")
        desk = db.desks.find_one({"name": DESK_NAME})
    stage = desk.get("incoming_stage") or desk.get("working_stage")
    return str(desk["_id"]), str(stage)


def build_doc(page, desk_id, stage_id, admin_id):
    doc = {
        "guid": page["guid"],
        "type": page.get("type", "text"),
        "profile": page["profile"],
        "state": "in_progress",
        "task": {"desk": desk_id, "stage": stage_id, "user": str(admin_id)},
    }
    for f in ("headline", "slugline", "language", "abstract", "body_html",
              "byline", "urgency", "priority", "extra", "subject"):
        if f in page:
            doc[f] = page[f]
    return doc


def import_pages(db, api, admin_id, data_dir):
    desk_id, stage_id = ensure_desk(db, api, admin_id)
    media_dir = Path(data_dir, "media")
    pages = sorted(Path(data_dir, "pages").glob("*.json"))
    created = published = skipped = failed = with_media = embedded_media = 0
    for path in pages:
        page = json.loads(path.read_text())
        guid = page["guid"]
        status, _ = api.get(f"archive/{guid}")
        if status == 200:
            skipped += 1
            continue
        doc = build_doc(page, desk_id, stage_id, admin_id)
        # Media: upload each tracked original (Superdesk regenerates renditions)
        # and attach the returned picture item as an association. `featuremedia`
        # is the hero image; the `embedded_media` map holds images embedded in
        # body_html, keyed by their editor block id (editor_0, ...). SWP's
        # EmbeddedImageProcessor rewrites each body <img src> to a public URL
        # using the matching association, so attaching them here is what makes
        # inline images render. A missing file just skips that one image.
        associations = {}
        fm_guid = page.get("feature_media")
        if fm_guid:
            media_path = find_media(media_dir, fm_guid)
            if media_path:
                pic = api.upload_picture(media_path, page.get("headline"))
                if pic:
                    associations["featuremedia"] = pic
                    with_media += 1
                else:
                    print(f"  media upload failed for {guid} ({fm_guid}); text-only")
            else:
                print(f"  feature media bytes missing for {guid} ({fm_guid}); text-only")
        for embed_id, pic_guid in sorted((page.get("embedded_media") or {}).items()):
            embed_path = find_media(media_dir, pic_guid)
            if not embed_path:
                print(f"  embedded media bytes missing for {guid} {embed_id} ({pic_guid})")
                continue
            epic = api.upload_picture(embed_path, page.get("headline"))
            if epic:
                associations[embed_id] = epic
                embedded_media += 1
            else:
                print(f"  embedded media upload failed for {guid} {embed_id} ({pic_guid})")
        if associations:
            doc["associations"] = associations
        status, res = api.post("archive", doc)
        if status not in (200, 201):
            print(f"  CREATE FAIL {guid}: {status} {res.get('_message') or res}")
            failed += 1
            continue
        created += 1
        # Publish: PATCH /archive/publish/{id} with the fresh etag.
        _, item = api.get(f"archive/{guid}")
        status, res = api.patch(f"archive/publish/{guid}", {"state": "published"}, item.get("_etag"))
        if status in (200, 201):
            published += 1
        else:
            print(f"  PUBLISH FAIL {guid}: {status} {res.get('_message') or res}")
            failed += 1
    print(f"Editorial pages: {created} created ({with_media} with feature media, "
          f"{embedded_media} embedded body images), {published} published, "
          f"{skipped} already present, {failed} failed (of {len(pages)}).")
    return failed


def main():
    base = os.environ.get("SUPERDESK_INTERNAL_API_URL", DEFAULT_API).rstrip("/")
    data_dir = os.environ.get("EDITORIAL_DATA_DIR", DEFAULT_DATA)
    db = superdesk_db()
    admin = db.users.find_one({"user_type": "administrator"}) or db.users.find_one()
    if not admin:
        raise SystemExit("no admin user; run the Superdesk bootstrap first")
    wait_for_api(base)
    api = Api(base, admin_token())
    failed = import_pages(db, api, admin["_id"], data_dir)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dump a running Superdesk's workflow config over its REST API.

The workflow analogue of `content_config/dump.sh`, for instances we can only
reach as an API user (no Mongo, no SSM): desks, stages, roles and their
privileges, users' role/privilege assignments, and the workflow add-ons that hang
off desks. Dump two instances with the same script and the diff between the two
output trees is the comparison — both sides go through the same API rendering,
so nothing in the diff is an artefact of raw-Mongo-vs-API formatting.

Read-only: it logs in, issues GETs only, and deletes its own session on exit.
Stdlib only, so it runs on the host.

Credentials never touch the command line or a file. The password is read with
`getpass` (hidden input); the session token lives only in memory. Set
SUPERDESK_TOKEN to reuse an existing session token instead of logging in (a raw
token, or a full `Basic ...` header value such as `manage.py
users:get_auth_token` prints).

The output holds usernames and display names, so write it outside the repo (the
a scratch directory) — it is review input, not a tracked file. Users are reduced to an
allow-list of workflow fields (USER_FIELDS); email, phone, password and
session data are never written.

The reference instance the tracked workflow was reconciled from (2026-09-28)
is the externally hosted pesacheck-staging.superdesk.pro, reachable only as an
API user. It is NOT the legacy `*.cfa2.superdesk.pro` EC2 hosts in our own AWS
account: their `sd-pesacheck-uat` database is an older deployment with one desk
and no roles.

Usage:
  ./dump_api.py --api https://pesacheck-staging-api.superdesk.pro/api --out DIR  # reference
  ./dump_api.py --api https://superdesk-staging.pesacheck.org/api --out DIR      # our staging

  --username (or SUPERDESK_USERNAME) skips the username prompt.

Output (DIR/): one `<resource>.json` per resource, a list sorted by `_id` with
sorted keys, plus `_meta.json` recording the source, the time, and any resource
the instance does not expose (404/403 — instances run different versions, so a
missing endpoint is recorded, not fatal).
"""

import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PAGE_SIZE = 200

# Workflow fields kept from a user. Everything else (email, phone, password,
# session/avatar data, sign-off, ...) is dropped: allow-list, not deny-list, so a
# new personal field in a newer core never leaks into a dump.
USER_FIELDS = {
    "_id",
    "username",
    "display_name",
    "role",
    "user_type",
    "privileges",
    "desk",
    "is_active",
    "is_enabled",
    "is_support",
    "is_author",
    "needs_activation",
}

# Bookkeeping that differs per instance and per save, never meaningful in a diff.
VOLATILE_FIELDS = {
    "_etag",
    "_links",
    "_updated",
    "_created",
    "_current_version",
    "_type",
}


def keep(fields):
    return lambda doc: {k: v for k, v in doc.items() if k in fields}


def template_summary(doc):
    data = doc.get("data") or {}
    out = {
        k: doc.get(k)
        for k in (
            "_id",
            "template_name",
            "template_type",
            "template_desks",
            "is_public",
        )
    }
    out["profile"] = data.get("profile")
    return out


# resource -> sanitizer (None = keep the whole doc, minus VOLATILE_FIELDS).
# content_types / content_templates are dumped only as id->name maps so desks'
# ObjectId references (content_profiles, default_content_template) can be
# resolved to names across instances; their content is content config, tracked
# separately under server/data/.
RESOURCES = {
    "desks": None,
    "stages": None,
    "roles": None,
    "privileges": None,
    "users": keep(USER_FIELDS),
    "macros": None,
    "routing_schemes": None,
    "content_filters": None,
    "filter_conditions": None,
    "saved_searches": None,
    "content_types": keep({"_id", "label", "enabled"}),
    "content_templates": template_summary,
}


class Client:
    def __init__(self, api):
        self.api = api.rstrip("/")
        self.auth = None

    def request(self, method, path, body=None, headers=None):
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        hdrs = {"Accept": "application/json", "User-Agent": "pesacheck-workflow-dump"}
        if self.auth:
            hdrs["Authorization"] = self.auth
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def use_token(self, token):
        # Already a header value, as `manage.py users:get_auth_token` prints it.
        if token.lower().startswith(("basic ", "bearer ")):
            self.auth = token
            return
        # Superdesk's TokenAuth reads the token as the Basic-auth username.
        self.auth = "Basic " + base64.b64encode(f"{token}:".encode()).decode()

    def login(self, username, password):
        session = self.request(
            "POST", "auth_db", {"username": username, "password": password}
        )
        self.use_token(session["token"])
        return session

    def logout(self, session):
        try:
            self.request(
                "DELETE",
                f"auth_db/{session['_id']}",
                headers={"If-Match": session.get("_etag", "")},
            )
        except (urllib.error.URLError, KeyError) as exc:
            log(f"warning: could not delete session ({exc}); it will expire on its own")

    def fetch_all(self, resource):
        docs, page = [], 1
        while True:
            query = urllib.parse.urlencode({"max_results": PAGE_SIZE, "page": page})
            resp = self.request("GET", f"{resource}?{query}")
            items = resp.get("_items", [])
            docs.extend(items)
            total = (resp.get("_meta") or {}).get("total", len(docs))
            if not items or len(docs) >= total:
                return docs
            page += 1


def log(msg):
    print(f">> {msg}", file=sys.stderr)


def sanitize(doc, fn):
    doc = fn(doc) if fn else doc
    return {k: v for k, v in doc.items() if k not in VOLATILE_FIELDS}


def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True, help="API base, e.g. https://host/api")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output directory (keep it outside the repo)",
    )
    parser.add_argument("--username", default=os.environ.get("SUPERDESK_USERNAME"))
    args = parser.parse_args()

    client = Client(args.api)
    session = None
    if os.environ.get("SUPERDESK_TOKEN"):
        client.use_token(os.environ["SUPERDESK_TOKEN"])
    else:
        username = args.username or input("Superdesk username: ")
        try:
            session = client.login(username, getpass.getpass("Superdesk password: "))
        except urllib.error.HTTPError as exc:
            sys.exit(f"error: login failed ({exc.code} {exc.reason})")
        log(f"Logged in to {client.api} as {username}")

    args.out.mkdir(parents=True, exist_ok=True)
    meta = {
        "api": client.api,
        "dumped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {},
        "unavailable": {},
    }
    try:
        for resource, fn in RESOURCES.items():
            try:
                docs = client.fetch_all(resource)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    sys.exit(
                        "error: the API rejected the session (401) — token expired or invalid"
                    )
                if exc.code in (403, 404, 405):
                    meta["unavailable"][resource] = exc.code
                    log(f"{resource}: unavailable ({exc.code})")
                    continue
                raise
            docs = sorted(
                (sanitize(d, fn) for d in docs), key=lambda d: str(d.get("_id", ""))
            )
            write_json(args.out / f"{resource}.json", docs)
            meta["counts"][resource] = len(docs)
            log(f"{resource}: {len(docs)}")
    finally:
        if session:
            client.logout(session)

    write_json(args.out / "_meta.json", meta)
    log(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

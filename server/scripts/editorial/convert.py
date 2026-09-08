#!/usr/bin/env python3
"""Convert a dump of authored editorial pages into the tracked fixture tree.

These are the Superdesk-authored pages (`source=newsdesk`) that the curated
Publisher content lists point at — About/FAQ/Team/Ecosystem/Media-Centre/etc.
Unlike Ghost fact-checks, nothing recreates them after a content reset, so they
are tracked as fixtures under `server/data/editorial/` and re-authored + published
by the importer at bootstrap, which puts them back into Publisher with their
ORIGINAL guids so the Publisher membership seeder
(`swp:config:seed-list-items`) can resolve them. See the plan
docs/plans/publisher-curated-list-membership.md (tier 3).

Pairs with `dump.sh`, which pulls the pages, their feature-media picture items and
the original media bytes from a running Superdesk. This converter writes:

    server/data/editorial/pages/<guid>.json      one text/page doc per file
    server/data/editorial/pictures/<guid>.json   one picture item per file
    (media bytes are written by dump.sh directly, tracked via Git LFS)

Input: `--source` dir holding `pages.json` and `pictures.json` (JSON arrays of
Superdesk docs, latest version per guid), as produced by dump.sh.

Profile-id remap: staging authored pages reference OLDER profile ids than the
tracked content-config installs. They are remapped to the canonical tracked ids
here so the fixtures validate after a content-config reseed. Profiles with NO
tracked equivalent (Page Section, Spotlight) are NOT remapped — the content-config
tree must carry them (they are captured by `make content-config-refresh`); this
converter asserts every profile it emits is either remapped or known-tracked and
fails loudly otherwise, so a missing profile is caught at capture, not at reset.
"""

import argparse
import json
from pathlib import Path

# Old (pre-canonical) profile ids -> the tracked content-config ids.
PROFILE_REMAP = {
    "6a954d10dae059933bb05432": "6a97ed55d0756a69fc29fab7",  # FAQ -> FAQ
    "6a96a3f9d0756a69fc29f90d": "6a97ebdad0756a69fc29fab0",  # Ecosystem -> Ecosystem Partner
    "6a96df4cd0756a69fc29f9ae": "6a97ecc6d0756a69fc29fab4",  # Team -> Team Member
    # Spotlight (6a8ef90a) is a DELETED profile — absent from content_types even on
    # staging (its 4 Media-Centre-Spotlight pages render blank there too). Remap to
    # Page Section, the generic media-bearing section card profile.
    "6a8ef90ae2b084181606ab39": "6a98515dd0756a69fc29fb06",  # Spotlight -> Page Section
}

# Profiles accepted as-is: the canonical tracked content_types plus core ones.
# Page Section / Spotlight MUST be added to the tracked content-config; list them
# here once they are, so capture stops rejecting them.
# Page Section pages require a page_section_role, stored as a `subject` entry
# (vocab qcodes: hero/section/cta). A few staging pages (notably the Spotlight
# pages remapped here) lack it, which blocks publish. Default the generic
# "section" role so they publish; hero/cta pages already carry their own entry.
PAGE_SECTION_PROFILE = "6a98515dd0756a69fc29fb06"
PAGE_SECTION_ROLE_SCHEME = "page_section_role"
DEFAULT_PAGE_SECTION_ROLE = {"name": "Section", "qcode": "section",
                             "scheme": PAGE_SECTION_ROLE_SCHEME}

TRACKED_PROFILES = {
    "6a8c9122e2b084181606a9ce",  # Announcement
    "6a8d9ff7e2b084181606aabb",  # Research Citations
    "6a97ebdad0756a69fc29fab0",  # Ecosystem Partner
    "6a97ecc6d0756a69fc29fab4",  # Team Member
    "6a97ed55d0756a69fc29fab7",  # FAQ
    "6a97edd7d0756a69fc29fabb",  # Event
    "6a98515dd0756a69fc29fb06",  # Page Section
    "article", "text", "picture", "composite", "audio", "video",
}

# Fields kept per page. Everything else (versions, task, queue_state, expiry,
# timestamps, *_creator, unique_id, etags) is per-instance churn and dropped.
PAGE_FIELDS = (
    "guid", "type", "profile", "headline", "slugline", "language",
    "abstract", "body_html", "byline", "priority", "urgency", "extra", "subject",
)
PICTURE_FIELDS = (
    "guid", "type", "profile", "headline", "description_text", "alt_text",
    "slugline", "language", "byline",
)

INDENT = "    "
COMPACT_LINE_MAX = 800


def remap_profile(pid):
    return PROFILE_REMAP.get(pid, pid)


def _compact(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


def emit(obj, indent=0):
    pad = INDENT * indent
    child = INDENT * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        keys = sorted(obj)
        lines = ["{"]
        for i, k in enumerate(keys):
            tail = "," if i < len(keys) - 1 else ""
            lines.append(f"{child}{json.dumps(k, ensure_ascii=False)}: {emit(obj[k], indent + 1)}{tail}")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = ["["]
        for i, v in enumerate(obj):
            tail = "," if i < len(obj) - 1 else ""
            compact = _compact(v)
            lines.append(f"{child}{compact}{tail}" if len(compact) <= COMPACT_LINE_MAX
                         else f"{child}{emit(v, indent + 1)}{tail}")
        lines.append(pad + "]")
        return "\n".join(lines)
    return json.dumps(obj, ensure_ascii=False)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit(obj) + "\n", encoding="utf-8")


def load_json(src, name):
    p = Path(src) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def featuremedia_guid(doc):
    assoc = (doc.get("associations") or {}).get("featuremedia") or {}
    return assoc.get("guid") or assoc.get("_id")


def pick(doc, fields):
    out = {}
    for f in fields:
        if f in doc and doc[f] not in (None, "", [], {}):
            out[f] = doc[f]
    return out


def convert(src, dest, summary):
    dest = Path(dest)
    for doc in load_json(src, "pages.json"):
        page = pick(doc, PAGE_FIELDS)
        pid = remap_profile(doc.get("profile"))
        if pid not in TRACKED_PROFILES:
            raise SystemExit(
                f"page {doc.get('guid')} uses profile {doc.get('profile')!r} with no "
                f"tracked/remap target — add it to content-config + TRACKED_PROFILES."
            )
        page["profile"] = pid
        if pid == PAGE_SECTION_PROFILE:
            subj = page.get("subject") or []
            if not any(s.get("scheme") == PAGE_SECTION_ROLE_SCHEME for s in subj):
                page["subject"] = subj + [dict(DEFAULT_PAGE_SECTION_ROLE)]
                summary["role_defaulted"] += 1
        fm = featuremedia_guid(doc)
        if fm:
            page["feature_media"] = fm  # picture guid; importer re-links after upload
        write_json(dest / "pages" / f"{doc['guid']}.json", page)
        summary["pages"] += 1

    for doc in load_json(src, "pictures.json"):
        pic = pick(doc, PICTURE_FIELDS)
        pic["profile"] = "picture"
        write_json(dest / "pictures" / f"{doc['guid']}.json", pic)
        summary["pictures"] += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="dir holding pages.json / pictures.json")
    ap.add_argument("--dest", default="data/editorial", help="output dir (default: data/editorial)")
    args = ap.parse_args(argv)
    summary = {"pages": 0, "pictures": 0, "role_defaulted": 0}
    convert(args.source, args.dest, summary)
    print(f"Pages:    {summary['pages']}")
    print(f"Pictures: {summary['pictures']}")
    print(f"page_section_role defaulted to 'section': {summary['role_defaulted']}")


if __name__ == "__main__":
    main()

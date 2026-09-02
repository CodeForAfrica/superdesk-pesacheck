#!/usr/bin/env python3
"""Convert a Superdesk content-config mongodump into the tracked JSON tree.

The content config lives as tracked JSON under `server/data/` (see AGENTS.md
§4). This script is the one-way converter that turns an opaque `mongodump` of the
config collections into those readable, reviewable files. Keeping it means a
future refresh from a running instance becomes *convert, then review a diff*
rather than a blind binary swap. Pairs with `dump.sh`.

Input:  a directory produced by `mongodump` (the folder holding `*.bson` and
        `*.metadata.json`, e.g. the `superdesk-content-config/<db>/` dir inside
        the dump tarball). Point `--source` at the tarball's extracted root or
        directly at that folder; the first directory containing `*.bson` wins.

Output: written under `--dest` (default `server/data`):

    vocabularies/editorial/<_id>.json     one file per editorial vocabulary
    vocabularies/reference/<_id>.json      one file per reference vocabulary
    content_types.json                     list, sorted by _id
    content_templates.json                 list, sorted by _id
    desks.json                             list, sorted by _id
    stages.json                            list, sorted by _id
    planning_types.json                    list, sorted by _id
    (content_filters / coverage_profiles: written only if non-empty)

Contract (acceptance criteria in the plan):
  * one JSON file per vocabulary, named for its `_id`;
  * item fields are never altered — conversion is faithful;
  * the `keywords` vocabulary is skipped: it accumulates at runtime on publish
    (KEYWORDS_ADD_MISSING_ON_PUBLISH) and must never be seeded from a file.

Determinism (so a re-dump produces a reviewable diff, not churn):
  * keys are sorted;
  * volatile bookkeeping fields (`_etag`, `_created`, `_updated`,
    `_current_version`) are stripped;
  * every list element short enough is emitted compact on a single line, so each
    vocabulary item is one line and a one-item change is a one-line diff; larger
    elements (e.g. a whole content profile) expand.

BSON types that have no JSON equivalent are rendered as the plain scalars core's
loader reconstructs: an ObjectId as its 24-character hex string, a datetime as an
ISO-8601 string. This is deliberate — core's `app:initialize_data` runs each doc
through `_mongotize`, which turns a 24-hex string back into an ObjectId but turns
Extended JSON (`{"$oid": ...}`) into a broken `{"": ObjectId(...)}`. So the tracked
files must NOT use Extended JSON.
"""

import argparse
import json
from pathlib import Path

# `bson` is imported lazily inside the functions that read BSON, so the pure
# formatting helpers (emit / write_json) can be reused where bson is absent.

# Runtime-accumulated vocabularies that must never be seeded from a tracked file.
DENY_VOCABULARIES = {"keywords"}

# Geographic reference data: the bulky, rarely-hand-edited lookup tables.
REFERENCE_VOCABULARIES = {
    "countries",
    "countrymention1",
    "countrymention2",
    "countrymention3",
    "countrymention4",
    "countrymention5",
    "regions",
    "locators",
}

# Per-instance / bookkeeping fields dropped on conversion.
VOLATILE_FIELDS = {
    "_etag",
    "_created",
    "_updated",
    "_current_version",
    "_links",
    "_type",
}

# Per-instance ownership / runtime state on the document collections.
PER_INSTANCE_FIELDS = {
    "created_by",
    "updated_by",
    "original_creator",
    "version_creator",
    "is_used",
    "members",
    "user",
}

# Non-vocabulary collections to convert, in output order. Value is the filename.
DOCUMENT_COLLECTIONS = {
    "content_types": "content_types.json",
    "content_templates": "content_templates.json",
    "desks": "desks.json",
    "stages": "stages.json",
    "planning_types": "planning_types.json",
    "content_filters": "content_filters.json",
    "coverage_profiles": "coverage_profiles.json",
}

# List elements whose compact form is at most this many characters are emitted on
# one line; longer ones expand. Chosen to keep vocabulary items on one line while
# expanding content profiles.
COMPACT_LINE_MAX = 800

INDENT = "    "


def to_plain(doc):
    """BSON document -> pure JSON structure that core's loader reconstructs.

    ObjectId -> 24-hex string, datetime -> ISO-8601 string. NOT Extended JSON:
    core's `_mongotize` reconstructs a hex string into an ObjectId but mangles
    `{"$oid": ...}` into `{"": ObjectId(...)}`.
    """
    import datetime as _dt

    from bson import ObjectId

    def conv(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: conv(v) for k, v in value.items()}
        if isinstance(value, list):
            return [conv(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise TypeError(f"unhandled BSON type {type(value).__name__}: {value!r}")

    return conv(doc)


def strip_volatile(obj, drop=VOLATILE_FIELDS):
    """Drop unwanted keys, recursively."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip_volatile(v, drop) for v in obj]
    return obj


def _compact(obj):
    """Deterministic single-line JSON for a value."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


def emit(obj, indent=0):
    """Deterministic pretty-printer: sorted keys, short list items on one line."""
    pad = INDENT * indent
    child = INDENT * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        keys = sorted(obj)
        lines = ["{"]
        for i, k in enumerate(keys):
            tail = "," if i < len(keys) - 1 else ""
            lines.append(
                f"{child}{json.dumps(k, ensure_ascii=False)}: "
                f"{emit(obj[k], indent + 1)}{tail}"
            )
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = ["["]
        for i, v in enumerate(obj):
            tail = "," if i < len(obj) - 1 else ""
            compact = _compact(v)
            if len(compact) <= COMPACT_LINE_MAX:
                lines.append(f"{child}{compact}{tail}")
            else:
                lines.append(f"{child}{emit(v, indent + 1)}{tail}")
        lines.append(pad + "]")
        return "\n".join(lines)
    return json.dumps(obj, ensure_ascii=False)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit(obj) + "\n", encoding="utf-8")


def load_bson(path):
    from bson import decode_file_iter

    with path.open("rb") as handle:
        return list(decode_file_iter(handle))


def find_dump_dir(source):
    """The first directory under `source` that contains a *.bson file."""
    source = Path(source)
    if list(source.glob("*.bson")):
        return source
    bson_files = sorted(source.rglob("*.bson"))
    if not bson_files:
        raise SystemExit(f"No *.bson files found under {source}")
    return bson_files[0].parent


def doc_id(doc):
    # to_plain() has already turned every ObjectId into a hex string, so _id is
    # always a plain scalar here (never Extended JSON) -- see to_plain's docstring.
    return str(doc.get("_id"))


def convert_vocabularies(src_dir, dest, summary):
    bson_path = src_dir / "vocabularies.bson"
    if not bson_path.exists():
        return
    for raw in load_bson(bson_path):
        doc = strip_volatile(to_plain(raw))
        vid = doc.get("_id")
        if not isinstance(vid, str):
            raise SystemExit(f"Vocabulary _id is not a string: {vid!r}")
        if vid in DENY_VOCABULARIES:
            summary["skipped"].append(vid)
            continue
        bucket = "reference" if vid in REFERENCE_VOCABULARIES else "editorial"
        write_json(dest / "vocabularies" / bucket / f"{vid}.json", doc)
        summary[bucket].append(vid)


def convert_documents(src_dir, dest, summary):
    for collection, filename in DOCUMENT_COLLECTIONS.items():
        bson_path = src_dir / f"{collection}.bson"
        if not bson_path.exists():
            continue
        docs = [
            strip_volatile(to_plain(d), VOLATILE_FIELDS | PER_INSTANCE_FIELDS)
            for d in load_bson(bson_path)
        ]
        if not docs:
            summary["empty"].append(collection)
            continue
        docs.sort(key=doc_id)
        write_json(dest / filename, docs)
        summary["documents"].append(f"{collection} ({len(docs)})")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source", required=True, help="mongodump directory or tarball-extracted root"
    )
    parser.add_argument(
        "--dest", default="data", help="output directory (default: data)"
    )
    args = parser.parse_args(argv)

    src_dir = find_dump_dir(args.source)
    dest = Path(args.dest)
    summary = {
        "editorial": [],
        "reference": [],
        "skipped": [],
        "documents": [],
        "empty": [],
    }

    convert_vocabularies(src_dir, dest, summary)
    convert_documents(src_dir, dest, summary)

    print(f"Source: {src_dir}")
    print(f"Dest:   {dest}")
    print(f"Editorial vocabularies: {len(summary['editorial'])}")
    print(f"Reference vocabularies: {len(summary['reference'])}")
    print(f"Skipped (deny-list):    {summary['skipped']}")
    print(f"Document collections:   {summary['documents']}")
    print(f"Empty (not written):    {summary['empty']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Measure what the Ghost tag mapping would populate, across a whole export.

Phase 5 of docs/plans/ghost-tag-field-mapping.md. Reports per-scheme coverage
and the unmatched tail, so the mapping in ``pesacheck/tag_vocabularies.py`` can
be judged against the real corpus rather than against a fixture -- and so the
coverage table in that plan and in ``pesacheck/tags.py`` can be re-derived
rather than trusted.

Importable without an app and without a database, like ``probe_image_host.py``:
it calls the real ``tag_subjects`` and the real ``is_ingestable_post``, so what
it reports is what ingest would do. A drifted copy would give a confidently
wrong answer, which is the same reason that probe shares its header builder.

    python scripts/ghost/probe_tag_mapping.py ~/exports/*.json
    python scripts/ghost/probe_tag_mapping.py export.json --with-gec
    python scripts/ghost/probe_tag_mapping.py export.json --leftovers 60

Two things it deliberately does not do: fetch anything, and rate a post. The
Debunk rating comes from the headline rather than the tags, so it is reported
separately and only to catch the Phase 0.1 class of defect -- a qcode the
vocabulary does not define.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

# The real thing, not a copy -- see the module docstring. Imported after the
# sys.path insert above, hence the E402 waivers.
from pesacheck.debunk import debunk_rating  # noqa: E402
from pesacheck.ghost_urls import is_ingestable_post  # noqa: E402
from pesacheck.tag_vocabularies import DROPPED_TAGS  # noqa: E402
from pesacheck.tags import (  # noqa: E402
    GEC,
    WIRED_SCHEMES,
    is_public_tag,
    normalise_tag,
    tag_subjects,
)

SCHEME_LABELS = {
    "countrymention1": "Primary country",
    "countries": "Countries mentioned",
    "Debunklang": "Debunk language",
    "content_type": "Content Type",
    "Harm_type": "Claim topic",
    "platform": "Primary platform",
    "GEC": "GEC category",
}


def read_export(path):
    """Yield ``(post, tags)`` for every ingestable post in one export file.

    Mirrors ``ghost_parser.parse``'s own join, including the null ``sort_order``
    coercion -- a post whose tags sort differently here would be measured
    against an order ingest never sees, and order decides every single-select
    field.
    """
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)["db"][0]["data"]

    tags_by_id = {t["id"]: t for t in data.get("tags", [])}
    tags_by_post = defaultdict(list)
    for row in data.get("posts_tags", []):
        tag = tags_by_id.get(row.get("tag_id"))
        if row.get("post_id") and tag:
            tags_by_post[row["post_id"]].append(
                {
                    "name": tag.get("name", ""),
                    "slug": tag.get("slug", ""),
                    "visibility": tag.get("visibility"),
                    "sort_order": row.get("sort_order") or 0,
                }
            )

    for post in data.get("posts", []):
        if not is_ingestable_post(post):
            continue
        tags = sorted(
            tags_by_post.get(post.get("id"), []), key=lambda t: t["sort_order"]
        )
        public = [tag for tag in tags if is_public_tag(tag)]
        yield post, public, len(tags) - len(public)


def percent(count, total):
    return f"{100.0 * count / total:5.1f}%" if total else "    -"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="Ghost export JSON files")
    parser.add_argument(
        "--with-gec",
        action="store_true",
        help="also report GEC, which is mapped but not wired into ingest",
    )
    parser.add_argument(
        "--leftovers", type=int, default=40, help="how many unmatched tags to list"
    )
    args = parser.parse_args()

    schemes = WIRED_SCHEMES + ((GEC,) if args.with_gec else ())

    posts = 0
    skipped_files = []
    posts_with_scheme = Counter()
    entries_per_scheme = Counter()
    qcodes_per_scheme = defaultdict(Counter)
    tag_applications = 0
    internal_applications = 0
    dropped_applications = 0
    posts_with_leftovers = 0
    leftover_applications = 0
    leftover_tags = Counter()
    rated = 0
    ratings = Counter()

    for path in args.paths:
        try:
            export = list(read_export(path))
        except (OSError, KeyError, ValueError, IndexError) as error:
            # One malformed file in a 301-file export should not lose the run.
            skipped_files.append((os.path.basename(path), error))
            continue

        for post, tags, internal in export:
            posts += 1
            tag_applications += len(tags)
            internal_applications += internal

            subjects, leftover = tag_subjects(tags, schemes=schemes)
            for scheme in {entry["scheme"] for entry in subjects}:
                posts_with_scheme[scheme] += 1
            for entry in subjects:
                entries_per_scheme[entry["scheme"]] += 1
                qcodes_per_scheme[entry["scheme"]][entry["qcode"]] += 1

            leftover_applications += len(leftover)
            leftover_tags.update(leftover)
            if leftover:
                posts_with_leftovers += 1
            dropped_applications += sum(
                1
                for tag in tags
                if {normalise_tag(tag.get("name")), normalise_tag(tag.get("slug"))}
                & DROPPED_TAGS
            )

            rating = debunk_rating(post.get("title") or "")
            if rating:
                rated += 1
                ratings[rating["qcode"]] += 1

    if not posts:
        print("No ingestable posts found.")
        return 1

    print(
        f"{len(args.paths)} file(s), {posts} ingestable post(s), "
        f"{tag_applications} public tag application(s)"
    )
    print(
        f"Excluded before mapping: {internal_applications} internal tag "
        f"application(s) (Ghost's own #Import bookkeeping); "
        f"{dropped_applications} self-referential one(s).\n"
    )

    print(
        f"{'Field':22s} {'Scheme':17s} {'Posts':>7s} {'Populated':>10s} {'Entries':>8s} {'qcodes':>7s}"
    )
    print("-" * 76)
    for scheme in schemes:
        print(
            f"{SCHEME_LABELS.get(scheme, scheme):22s} {scheme:17s} "
            f"{posts_with_scheme[scheme]:7d} {percent(posts_with_scheme[scheme], posts):>10s} "
            f"{entries_per_scheme[scheme]:8d} {len(qcodes_per_scheme[scheme]):7d}"
        )
    print("-" * 76)
    # `Populated` is deliberately a different denominator on this row: what
    # matters about the tail is the share of tag applications it is, not the
    # share of posts that have one.
    print(
        f"{'remainder to keywords':22s} {'-':17s} "
        f"{posts_with_leftovers:7d} {percent(leftover_applications, tag_applications):>10s} "
        f"{leftover_applications:8d} {len(leftover_tags):7d}"
        "   <- % of tag applications"
    )
    print(
        f"\nDebunk rating: {rated} post(s) rated, {percent(rated, posts)} of the corpus"
    )
    for qcode, count in ratings.most_common():
        print(f"    {qcode:12s} {count:6d}")

    print(f"\nTop {args.leftovers} unmatched tag(s) -- these become keywords:")
    for name, count in leftover_tags.most_common(args.leftovers):
        print(f"    {count:6d}  {name}")
    print(f"\n{len(leftover_tags)} distinct unmatched tag(s) in total.")

    for scheme in schemes:
        print(f"\n{scheme} qcode distribution:")
        for qcode, count in qcodes_per_scheme[scheme].most_common():
            print(f"    {count:6d}  {qcode}")

    if skipped_files:
        # Never silent: a truncated corpus reads as coverage if it is not said.
        print(f"\nSKIPPED {len(skipped_files)} unreadable file(s):")
        for name, error in skipped_files:
            print(f"    {name}: {type(error).__name__}: {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

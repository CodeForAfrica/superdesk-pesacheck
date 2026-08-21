#!/usr/bin/env python3
"""Offline profile of a Ghost export: how many images, from which hosts.

Phase 0 of the ingest speed-up. Answers, without touching the cluster, the one
question that decides where to spend effort: is the run dominated by the Medium
throttle (a config fix) or by raw per-image work (needs concurrency and CPU)?

Mirrors GhostParser's selection logic exactly so the counts match what ingest
will actually fetch:

* only ``status == "published"`` and ``type == "post"`` (GhostParser.iter_items)
* feature image from ``feature_image``, inline images from ``.//img`` in ``html``
* ``__GHOST_URL__`` substituted, then anything still relative is skipped
  (GhostParser._resolve_url)
* Medium classified as "medium.com" appearing anywhere in the hostname
  (GhostParser._is_medium_cdn_url)

Usage:
    python scripts/ghost/profile_export.py <export.json> [more.json ...] \
        --url https://pesacheck.org [--interval 0.1] [--medium-interval 3.0]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

# Shared with the parser so the counts here match what ingest will actually
# fetch. pesacheck.ghost_urls imports nothing from superdesk, so it loads without
# an app (and without libmagic) — which is the point of these offline tools.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pesacheck.ghost_urls import (  # noqa: E402
    GHOST_URL_PLACEHOLDER,
    is_ingestable_post,
    is_medium_cdn_url,
    resolve_url,
)

# Cheap stand-in for lxml's .//img xpath. The parser uses a real HTML parse, so
# this can differ on pathological markup; --strict swaps in lxml to check.
_IMG_SRC = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE | re.DOTALL
)


def extract_img_srcs(html, strict=False):
    if not html:
        return []
    if strict:
        from lxml import html as lxml_html

        try:
            return [
                el.get("src")
                for el in lxml_html.fromstring(html).xpath(".//img")
                if el.get("src")
            ]
        except Exception:
            return []
    return _IMG_SRC.findall(html)


def profile(paths, ghost_url, strict=False):
    stats = {
        "files": 0,
        "posts_total": 0,
        "posts_ingested": 0,
        "posts_no_image": 0,
        "img_refs": 0,
        "img_skipped_relative": 0,
        "feature_refs": 0,
        "inline_refs": 0,
    }
    hosts = Counter()
    url_uses = Counter()
    per_post_counts = Counter()

    for path in paths:
        stats["files"] += 1
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        posts = data["db"][0]["data"].get("posts", [])
        stats["posts_total"] += len(posts)

        for post in posts:
            if not is_ingestable_post(post):
                continue
            stats["posts_ingested"] += 1

            html = post.get("html") or ""
            if ghost_url and html:
                html = html.replace(GHOST_URL_PLACEHOLDER, ghost_url)

            raw = [(post.get("feature_image"), "feature")]
            raw += [(src, "inline") for src in extract_img_srcs(html, strict)]

            kept = 0
            for raw_url, kind in raw:
                if not raw_url:
                    continue
                stats["img_refs"] += 1
                url = resolve_url(raw_url, ghost_url)
                if not url:
                    stats["img_skipped_relative"] += 1
                    continue
                kept += 1
                stats[f"{kind}_refs"] += 1
                url_uses[url] += 1
                hosts[(urlparse(url).hostname or "?").lower()] += 1

            per_post_counts[kept] += 1
            if kept == 0:
                stats["posts_no_image"] += 1

    return stats, hosts, url_uses, per_post_counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--url", default="", help="provider config 'url' (Ghost site)")
    ap.add_argument("--strict", action="store_true", help="use lxml, not regex")
    ap.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="GHOST_IMAGE_FETCH_MIN_INTERVAL in effect (default 0.1)",
    )
    ap.add_argument(
        "--medium-interval",
        type=float,
        default=3.0,
        help="GHOST_MEDIUM_FETCH_MIN_INTERVAL in effect (default 3.0)",
    )
    args = ap.parse_args()

    if not args.url:
        print(
            "warning: no --url given, so __GHOST_URL__ images count as skipped\n",
            file=sys.stderr,
        )

    stats, hosts, url_uses, per_post = profile(
        args.paths, args.url.rstrip("/"), args.strict
    )

    fetches = sum(url_uses.values())
    unique = len(url_uses)
    medium_unique = sum(1 for u in url_uses if is_medium_cdn_url(u))
    medium_fetches = sum(n for u, n in url_uses.items() if is_medium_cdn_url(u))
    posts = stats["posts_ingested"] or 1

    print("== corpus ==")
    print(f"  files                  {stats['files']}")
    print(f"  posts in export        {stats['posts_total']}")
    print(f"  posts ingested         {stats['posts_ingested']}")
    print(f"  posts with no image    {stats['posts_no_image']}")
    print()
    print("== images ==")
    print(f"  <img>/feature refs     {stats['img_refs']}")
    print(f"  skipped (relative)     {stats['img_skipped_relative']}")
    print(f"  fetchable refs         {fetches}   ({fetches / posts:.2f} per post)")
    print(f"    of which feature     {stats['feature_refs']}")
    print(f"    of which inline      {stats['inline_refs']}")
    print(f"  distinct URLs          {unique}")
    print(
        f"  duplicate refs         {fetches - unique}  <- free if the cache spans files"
    )
    print()
    print("== hosts (top 15) ==")
    for host, n in hosts.most_common(15):
        tag = "  [MEDIUM]" if "medium.com" in host else ""
        print(f"  {n:8d}  {host}{tag}")
    print()
    print("== medium exposure ==")
    pct = 100.0 * medium_fetches / fetches if fetches else 0.0
    print(f"  medium fetches         {medium_fetches} / {fetches}  ({pct:.1f}%)")
    print(f"  medium distinct URLs   {medium_unique}")
    print()
    print("== images per post ==")
    for k in sorted(per_post):
        print(f"  {k:3d} image(s)  {per_post[k]:6d} posts")
    print()

    # Floor from the pacers alone: what the run costs before any download, resize
    # or S3 upload happens. Uses distinct URLs, since a cache hit does not sleep.
    non_medium_unique = unique - medium_unique
    sleep_s = non_medium_unique * args.interval + medium_unique * args.medium_interval
    print("== throttle floor (sleeps only, serial, cache warm) ==")
    print(
        f"  non-medium  {non_medium_unique} x {args.interval}s   = {non_medium_unique * args.interval / 3600:.2f} h"
    )
    print(
        f"  medium      {medium_unique} x {args.medium_interval}s = {medium_unique * args.medium_interval / 3600:.2f} h"
    )
    print(f"  TOTAL                            = {sleep_s / 3600:.2f} h")
    print()
    print("  Compare against your observed wall clock. If this is most of it,")
    print("  Phase 1 (env knobs) is the whole fix. If it is a small fraction,")
    print("  the cost is per-image work and you need Phase 2+3 concurrency.")


if __name__ == "__main__":
    main()

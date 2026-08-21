#!/usr/bin/env python3
"""Find the sustainable fetch rate for an image host. Phase 0b of the speed-up.

The export profile showed 91% of images live on cdn-images-1.medium.com, so the
whole backfill is governed by what that CDN tolerates — not by our CPU. The
parser currently paces Medium at one request every 3.0s
(GHOST_MEDIUM_FETCH_MIN_INTERVAL), which costs ~70 hours for 84k distinct URLs.
That 3.0s was chosen when Medium images were 403-ing, but a same-origin Referer
was introduced later for a different 403 cause, so the pacer may be mitigating a
bug that no longer exists. This measures instead of guessing.

Sends byte-for-byte the same request ingest sends: the parser's own
_IMAGE_FETCH_HEADERS plus the per-request same-origin Referer from
_build_fetch_headers. Results only transfer if the request shape matches.

Walks a concurrency ladder and stops at the first level where the error rate
crosses --max-error-rate, so it finds the ceiling without hammering past it.
Deliberately does NOT retry: retries would mask the very rate limiting we are
trying to detect.

    python scripts/ghost/probe_image_host.py export.json \
        --url https://pesacheck.org --host cdn-images-1.medium.com

Read the output as an upper bound, not a guarantee: a CDN that tolerates 16-way
concurrency for 300 requests may still throttle after 80,000. Whatever number
this suggests, run the real fetch with retry and resume.
"""

import argparse
import json
import os
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
from profile_export import extract_img_srcs  # noqa: E402

# The real thing, not a copy. This probe's whole claim is that it sends what
# ingest sends — a drifted copy of the headers would produce a rate the actual
# fetcher cannot achieve, and the Referer specifically has caused one outage.
from pesacheck.ghost_urls import (  # noqa: E402
    build_fetch_headers as headers_for,
    is_ingestable_post,
    resolve_url,
)


def collect_urls(paths, ghost_url, host):
    """Distinct fetchable URLs on ``host``, in export order."""
    seen = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for post in data["db"][0]["data"].get("posts", []):
            if not is_ingestable_post(post):
                continue
            html = post.get("html") or ""
            if ghost_url and html:
                html = html.replace("__GHOST_URL__", ghost_url)
            for raw in [post.get("feature_image")] + extract_img_srcs(html):
                url = resolve_url(raw, ghost_url)
                if not url:
                    continue
                if host and host not in (urlparse(url).hostname or ""):
                    continue
                seen.setdefault(url, None)
    return list(seen)


def fetch(session, url, timeout):
    start = time.monotonic()
    try:
        resp = session.get(url, headers=headers_for(url), timeout=timeout, stream=True)
        size = len(resp.content)
        return resp.status_code, time.monotonic() - start, size, None
    except requests.Timeout:
        return "timeout", time.monotonic() - start, 0, None
    except Exception as ex:
        return "error", time.monotonic() - start, 0, repr(ex)[:120]


def run_level(urls, concurrency, timeout):
    codes = Counter()
    latencies = []
    total_bytes = 0
    errors = []
    started = time.monotonic()

    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for code, elapsed, size, err in pool.map(
                lambda u: fetch(session, u, timeout), urls
            ):
                codes[code] += 1
                latencies.append(elapsed)
                total_bytes += size
                if err:
                    errors.append(err)

    wall = time.monotonic() - started
    ok = codes.get(200, 0)
    return {
        "concurrency": concurrency,
        "requests": len(urls),
        "wall": wall,
        "rps": len(urls) / wall if wall else 0.0,
        "ok": ok,
        "error_rate": 1.0 - (ok / len(urls)) if urls else 0.0,
        "codes": codes,
        "p50": statistics.median(latencies) if latencies else 0.0,
        "p95": (
            statistics.quantiles(latencies, n=20)[18]
            if len(latencies) >= 20
            else max(latencies, default=0.0)
        ),
        "mib": total_bytes / (1024 * 1024),
        "errors": errors[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--url", default="", help="provider config 'url' (Ghost site)")
    ap.add_argument("--host", default="cdn-images-1.medium.com")
    ap.add_argument("--per-level", type=int, default=60, help="requests per rung")
    ap.add_argument("--ladder", default="1,2,4,8,16,32")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--max-error-rate", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    urls = collect_urls(args.paths, args.url.rstrip("/"), args.host)
    if not urls:
        print(f"no fetchable URLs found on {args.host!r}", file=sys.stderr)
        return 1

    ladder = [int(x) for x in args.ladder.split(",")]
    # Sample rather than taking the head: the first N URLs of an export are all
    # from the same few posts and may share a cache shard, which flatters latency.
    random.Random(args.seed).shuffle(urls)

    print(f"host {args.host}   distinct URLs available {len(urls)}")
    print(f"probing {args.per_level} requests per level, ladder {ladder}")
    print(
        f"abort when error rate > {args.max_error_rate:.0%}   (no retries, by design)"
    )
    print()
    hdr = f"{'conc':>5} {'reqs':>6} {'wall':>7} {'req/s':>7} {'ok':>6} {'err%':>6} {'p50':>7} {'p95':>7} {'MiB':>7}"
    print(hdr)
    print("-" * len(hdr))

    results = []
    cursor = 0
    for concurrency in ladder:
        batch = urls[cursor : cursor + args.per_level]
        cursor += args.per_level
        if len(batch) < args.per_level:
            print(f"(only {len(batch)} URLs left; stopping ladder)")
            break

        r = run_level(batch, concurrency, args.timeout)
        results.append(r)
        print(
            f"{r['concurrency']:5d} {r['requests']:6d} {r['wall']:6.1f}s "
            f"{r['rps']:7.2f} {r['ok']:6d} {100 * r['error_rate']:5.1f}% "
            f"{r['p50']:6.2f}s {r['p95']:6.2f}s {r['mib']:7.1f}"
        )
        nonok = {k: v for k, v in r["codes"].items() if k != 200}
        if nonok:
            print(f"        non-200: {dict(nonok)}")
        for err in r["errors"]:
            print(f"        {err}")

        if r["error_rate"] > args.max_error_rate:
            print(
                f"\nSTOP: error rate {100 * r['error_rate']:.1f}% at concurrency {concurrency}."
            )
            break

    clean = [r for r in results if r["error_rate"] <= args.max_error_rate]
    if not clean:
        print("\nNo clean level. The host is rejecting us even serially — check the")
        print("Referer/User-Agent, or you are already rate limited. Wait and retry.")
        return 2

    best = max(clean, key=lambda r: r["rps"])
    total = len(urls)
    hours = total / best["rps"] / 3600 if best["rps"] else 0.0
    print()
    print("== recommendation ==")
    print(
        f"  best clean level      concurrency {best['concurrency']}, {best['rps']:.2f} req/s"
    )
    print(f"  {total} distinct URLs  -> {hours:.2f} h at that rate")
    print(f"  vs current 3.0s pacer -> {total * 3.0 / 3600:.2f} h")
    print()
    print("  Interpretation: this is a short burst, not a sustained load test. Treat")
    print("  the number as a ceiling, halve it for the real run, and make that run")
    print("  resumable so a mid-run block costs a resume and not a restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

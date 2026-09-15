"""Build the modern layer in bulk, for the 35,073 titles the 1911 layer aligns to.

`s04` fetches one article per call, which will not clear the anonymous Wikimedia
quota at this scale. Two bulk routes exist and they are not close:

**batch** (the default). `prop=revisions` returns full wikitext for **50 titles
per call**, in about a second, with no `exlimit` trap of the kind that made
`prop=extracts` quietly return lead sections only. 35,073 titles is therefore
about 700 requests. Wikitext comes back instead of rendered text, which is fine,
because `wikitext.py` already converts it for the 1911 layer.

**dump**. The same selective multistream trick `s03b` uses on Wikisource. Priced
against the real index rather than guessed at:

    English Wikipedia multistream        26.80 GB  (plus a 0.28 GB index)
    streams in the dump                   258,634
    streams holding a wanted title         15,244  (5.9%)
    ranges after merging at 256 KB          6,257  ->  3.84 GB, 14% of the dump

I expected worse. The prediction was that these titles, written by unrelated
people over twenty years, would be scattered across nearly every stream and the
plan would come out at a large fraction of the dump. It comes out at 14%,
because page id order is roughly creation order and the articles a 1911
encyclopaedia aligns to are mostly old ones, written early and therefore sitting
near each other in the file.

The batch route still wins, on both axes rather than narrowly: 727 calls against
6,257, and 1.4 GB against 3.84 GB plus the index. But the dump route is a real
fallback rather than a straw man, and it is the one to use if the API quota ever
closes this door the way it closed `s04`'s. `--route dump --phases plan` prints
the table above without fetching anything, which is the honest way to choose.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time

import multistream as ms
import wikitext as wt
from common import (EB1911_DB, INTERIM, RAW, RateLimiter, WIKIPEDIA_API, api_get,
                    get_logger, session)

log = get_logger("s04b")

DB = INTERIM / "modern.sqlite"
DUMP_BASE = "https://dumps.wikimedia.org/enwiki/latest/"
INDEX_FILE = "enwiki-latest-pages-articles-multistream-index.txt.bz2"
MULTISTREAM_FILE = "enwiki-latest-pages-articles-multistream.xml.bz2"
BATCH = 50  # the API maximum for prop=revisions without bot rights

_REDIRECT = re.compile(r"^\s*#redirect\s*\[\[([^\]]+)\]\]", re.IGNORECASE)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=120)
    con.execute("""CREATE TABLE IF NOT EXISTS pages (
        requested TEXT PRIMARY KEY, title TEXT, pageid INTEGER, text TEXT,
        chars INTEGER, missing INTEGER, source TEXT, fetched REAL)""")
    return con


def wanted_titles() -> list[tuple[str, str]]:
    """(title to fetch, where the alignment came from), deduplicated.

    The `wikipedia=` parameter in the EB1911 header is the curated route and
    covers 95% of the layer. The rest fall back to the 1911 headword, which is
    right more often than not and wrong in a way that shows up as a missing page
    rather than as a silently mismatched one.
    """
    if not EB1911_DB.exists():
        raise SystemExit("run s03b (or s03) first: no 1911 layer to align against")
    src = sqlite3.connect(EB1911_DB)
    out, seen = [], set()
    for headword, wp in src.execute("SELECT headword, wikipedia FROM articles"):
        title = wp or headword
        if not title or title in seen:
            continue
        seen.add(title)
        out.append((title, "eb1911_param" if wp else "headword_match"))
    return out


def store(con: sqlite3.Connection, rows: list[tuple]) -> None:
    con.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()


def fetch_batch(sess, lim: RateLimiter, chunk: list[str], src_of: dict) -> list[tuple]:
    """One call, 50 titles, full wikitext, converted to prose."""
    js = api_get(sess, WIKIPEDIA_API, {
        "action": "query", "prop": "revisions", "rvprop": "content",
        "rvslots": "main", "redirects": 1, "titles": "|".join(chunk)}, limiter=lim)
    q = js.get("query", {})
    # `normalized` and `redirects` rewrite titles on the way out, so map answers
    # back to what was asked for. Without this, resumption re-requests the same
    # pages for ever, because nothing ever lands under the key it looked up.
    back: dict[str, str] = {}
    for kind in ("normalized", "redirects"):
        for m in q.get(kind, []):
            back[m["to"]] = back.get(m["from"], m["from"])
    rows = []
    for page in q.get("pages", []):
        title = page.get("title", "")
        requested = back.get(title, title)
        revs = page.get("revisions") or [{}]
        body = (revs[0].get("slots", {}).get("main", {}).get("content")) or ""
        text = wt.to_plain_text(body) if body else ""
        rows.append((requested, title, page.get("pageid"), text, len(text),
                     int(bool(page.get("missing"))), src_of.get(requested, ""),
                     time.time()))
    return rows


def run_batch(con: sqlite3.Connection, per_minute: float, limit: int) -> None:
    have = {t for (t,) in con.execute("SELECT requested FROM pages")}
    todo = [(t, s) for t, s in wanted_titles() if t not in have]
    if limit:
        todo = todo[:limit]
    log.info("%d modern pages stored, %d to fetch, %d calls at %d titles each",
             len(have), len(todo), -(-len(todo) // BATCH), BATCH)
    if not todo:
        return

    src_of = dict(todo)
    sess, lim = session(), RateLimiter(per_minute)
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = [t for t, _ in todo[i:i + BATCH]]
        try:
            rows = fetch_batch(sess, lim, chunk, src_of)
        except Exception as e:  # noqa: BLE001
            log.warning("batch at %d failed: %s", i, e)
            continue
        store(con, rows)
        n = i + len(chunk)
        if (i // BATCH) % 20 == 0 or n >= len(todo):
            done, chars = con.execute(
                "SELECT COUNT(*), SUM(chars) FROM pages").fetchone()
            log.info("%d/%d titles, %d stored, %.0f MB, %.1f titles/s",
                     n, len(todo), done, (chars or 0) / 1e6,
                     n / max(time.time() - t0, 1e-6))
    report(con)


def report(con: sqlite3.Connection) -> None:
    n, missing, chars = con.execute(
        "SELECT COUNT(*), SUM(missing), SUM(chars) FROM pages").fetchone()
    present = n - (missing or 0)
    log.info("%d titles requested, %d have no article, %.0f MB of prose, %.1f KB each",
             n, missing or 0, (chars or 0) / 1e6, (chars or 0) / 1e3 / max(present, 1))


def run_dump_plan(merge_gap: int, do_fetch: bool, per_minute: float,
                  limit: int) -> None:
    """Price the dump route, and run it if that price is acceptable."""
    sess = session()
    index_path = RAW / INDEX_FILE
    ms.ensure_file(sess, DUMP_BASE + INDEX_FILE, index_path, log)
    dump_size = ms.remote_size(sess, DUMP_BASE + MULTISTREAM_FILE)

    want_set = {t for t, _ in wanted_titles()}
    plan = ms.build_plan(index_path, lambda t: t in want_set, dump_size, merge_gap)
    log.info("plan: %s", plan.summary())
    # Measured, not guessed: a 50 title call returned 2.02 MB of wikitext in
    # 1.1 seconds, so the comparison below is arithmetic on an observed rate.
    calls = -(-len(want_set) // BATCH)
    log.info("compare: the batch route is %d calls, about %.1f GB, at the "
             "measured 2.0 MB per 50 title call", calls, calls * 2.02 / 1024)
    if not do_fetch:
        log.info("stopping at the plan; add --phases plan,fetch to run it")
        return

    con = connect()
    url = DUMP_BASE + MULTISTREAM_FILE
    lim = RateLimiter(per_minute)
    runs = plan.runs[:limit] if limit else plan.runs
    kept = 0
    for i, (start, end) in enumerate(runs, 1):
        lim.wait()
        try:
            text = ms.fetch_range(sess, url, start, end)
        except OSError as e:
            log.warning("range %d did not decompress: %s", i, e)
            continue
        rows = []
        for title, body in ms.iter_pages(text):
            if title not in want_set:
                continue
            m = _REDIRECT.match(body or "")
            if m:  # follow it on the next pass rather than storing an empty page
                continue
            prose = wt.to_plain_text(body)
            rows.append((title, title, None, prose, len(prose), 0, "dump", time.time()))
        if rows:
            store(con, rows)
            kept += len(rows)
        if i % 50 == 0 or i == len(runs):
            log.info("%d/%d ranges, %d articles kept", i, len(runs), kept)
    report(con)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--route", choices=["batch", "dump"], default="batch")
    ap.add_argument("--phases", default="plan",
                    help="dump route only: plan, or plan,fetch")
    ap.add_argument("--per-minute", type=float, default=120)
    ap.add_argument("--limit", type=int, default=0,
                    help="batch: stop after N titles. dump: stop after N ranges")
    ap.add_argument("--merge-gap", type=int, default=256 * 1024)
    args = ap.parse_args()

    if args.route == "batch":
        run_batch(connect(), args.per_minute, args.limit)
    else:
        run_dump_plan(args.merge_gap, "fetch" in args.phases, args.per_minute,
                      args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

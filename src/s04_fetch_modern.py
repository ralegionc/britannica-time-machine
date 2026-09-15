"""Fetch the modern layer one article at a time. Superseded by `s04b`.

`s04b` does the same job 50 titles per call through `prop=revisions`, which is
roughly 700 requests instead of 35,073. This stage is kept because it is the
comparison case, and because the trap documented below is worth not walking into
twice.

Alignment comes from three places, in order of trust:
  1. the `wikipedia=` parameter the Wikisource editors put in the EB1911 header,
  2. an exact title match for the 1911 headword,
  3. nothing, in which case the entry has no modern layer and the question set
     treats it as a disappearance (s09 reports these; they are interesting).

Wikipedia's `prop=extracts` works normally here, unlike on Wikisource, but it
has a trap: with `exlimit` above 1 it returns **lead sections only**, silently
and with no indication in the response. Batched at 20 titles per call the modern
layer came back at about 1.2 KB per article, which is an abstract, not a corpus.
Full text needs one call per page. Wikipedia answers those in a few hundred
milliseconds, so the stage runs them concurrently; `--intro-only` restores the
fast batched behaviour when lead sections are all that is wanted.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from common import (EB1911_DB, INTERIM, RateLimiter, WIKIPEDIA_API, api_get,
                    get_logger, session)

log = get_logger("s04")
DB = INTERIM / "modern.sqlite"
BATCH = 20  # only used by --intro-only


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=60)
    con.execute("""CREATE TABLE IF NOT EXISTS pages (
        requested TEXT PRIMARY KEY, title TEXT, pageid INTEGER, text TEXT,
        chars INTEGER, missing INTEGER, source TEXT, fetched REAL)""")
    return con


def wanted_titles() -> list[tuple[str, str]]:
    """(title to request, where the alignment came from)."""
    if not EB1911_DB.exists():
        raise SystemExit("run s03 first: no 1911 layer to align against")
    src = sqlite3.connect(EB1911_DB)
    out: list[tuple[str, str]] = []
    for headword, wp in src.execute("SELECT headword, wikipedia FROM articles"):
        if wp:
            out.append((wp, "eb1911_param"))
        elif headword:
            out.append((headword, "headword_match"))
    seen, uniq = set(), []
    for t, s in out:
        if t not in seen:
            seen.add(t)
            uniq.append((t, s))
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    # Wikipedia answers anonymous clients with HTTP 429 well below the rates
    # its documentation implies, and it does so under concurrency more than
    # under throughput. Two workers at 100 calls a minute runs clean; four at
    # 300 spent its time in Retry-After backoff and stored nothing.
    ap.add_argument("--per-minute", type=float, default=100)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--intro-only", action="store_true",
                    help="20 titles per call, but lead sections only")
    args = ap.parse_args()

    con = connect()
    have = {t for (t,) in con.execute("SELECT requested FROM pages")}
    todo = [(t, s) for t, s in wanted_titles() if t not in have]
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d modern pages stored, %d to fetch", len(have), len(todo))

    sess, lim = session(), RateLimiter(args.per_minute)
    src_of = dict(todo)

    def store(rows) -> None:
        con.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()

    def unpack(js: dict, requested_for) -> list[tuple]:
        q = js.get("query", {})
        # `redirects` and `normalized` rewrite titles, so map answers back to what
        # was asked for; otherwise resumption re-requests the same pages forever.
        back = {}
        for kind in ("normalized", "redirects"):
            for m in q.get(kind, []):
                back[m["to"]] = back.get(m["from"], m["from"])
        rows = []
        for page in q.get("pages", []):
            title = page.get("title", "")
            requested = back.get(title, requested_for(title))
            text = page.get("extract") or ""
            rows.append((requested, title, page.get("pageid"), text, len(text),
                         int(bool(page.get("missing"))), src_of.get(requested, ""),
                         time.time()))
        return rows

    if args.intro_only:
        for i in range(0, len(todo), BATCH):
            chunk = [t for t, _ in todo[i:i + BATCH]]
            js = api_get(sess, WIKIPEDIA_API, {
                "action": "query", "prop": "extracts", "explaintext": 1,
                "exlimit": "max", "redirects": 1, "titles": "|".join(chunk)}, limiter=lim)
            store(unpack(js, lambda t: t))
            if (i // BATCH) % 25 == 0:
                log.info("%d/%d", i + len(chunk), len(todo))
    else:
        def fetch(item) -> list[tuple]:
            title, _ = item
            try:
                js = api_get(sess, WIKIPEDIA_API, {
                    "action": "query", "prop": "extracts", "explaintext": 1,
                    "exlimit": 1, "redirects": 1, "titles": title}, limiter=lim)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: %s", title, e)
                return []
            return unpack(js, lambda _t: title)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            batch: list[tuple] = []
            for n, rows in enumerate(pool.map(fetch, todo), 1):
                batch += rows
                if len(batch) >= 100:
                    store(batch)
                    batch = []
                    log.info("%d/%d", n, len(todo))
            store(batch)

    n, missing, chars = con.execute(
        "SELECT COUNT(*), SUM(missing), SUM(chars) FROM pages").fetchone()
    log.info("%d requested, %d have no modern article, %.1f MB of text (%.1f KB each)",
             n, missing or 0, (chars or 0) / 1e6, (chars or 0) / 1e3 / max(n - (missing or 0), 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

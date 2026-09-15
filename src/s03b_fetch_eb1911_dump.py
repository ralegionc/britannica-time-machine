"""Build the whole 1911 layer from the Wikisource dump instead of the API.

`s03` asks the API to render one article at a time. That is the right tool for a
sample and the wrong one for an edition: the anonymous Wikimedia quota is shared
across projects and shut this host out of both Wikipedia and Wikisource after 250
articles, and a cold `action=parse` on a transcluded page costs up to 25 seconds
of somebody else's CPU. The dump has no quota and no rendering.

The naive dump route downloads 3.64 GB. This one downloads a third of that,
because the multistream dump is seekable: it is a concatenation of independent
bzip2 streams of 100 pages each, and the published index says which stream every
page is in (see multistream.py). Measured on the September 2026 dump:

    index lines                    4,751,918
    EB1911 articles                   38,771
    Page:EB1911 scan records          28,784
    streams in the dump               47,521
    streams holding EB1911 material   10,389

Those streams are 0.85 GB in 4,290 separate ranges. Merging ranges less than
256 KB apart, the default, makes it 1.15 GB in 1,477 requests, which is the
better trade against a file server. `--merge-gap 0` takes the other one.

The whole fetch took 12 minutes and recovered exactly the planned counts.

Four phases, each resumable, each skippable:

    index    download the 31 MB index (once)
    plan     scan the index, work out which byte ranges are needed
    fetch    pull those ranges, decompress, keep the EB1911 pages
    resolve  reconstruct each article from its scan pages (see wikitext.py)

`--full` ignores all of that and streams the ordinary 3.4 GB dump end to end,
which is one request rather than thousands if that suits the server better.
"""
from __future__ import annotations

import argparse
import bz2
import re
import sqlite3
import statistics
import sys
import time

import requests

import multistream as ms
import wikitext as wt
from common import INTERIM, RAW, RateLimiter, eb1911_connect, get_logger, session

log = get_logger("s03b")

DUMP_BASE = "https://dumps.wikimedia.org/enwikisource/latest/"
INDEX_FILE = "enwikisource-latest-pages-articles-multistream-index.txt.bz2"
MULTISTREAM_FILE = "enwikisource-latest-pages-articles-multistream.xml.bz2"
FULL_FILE = "enwikisource-latest-pages-articles.xml.bz2"

DB = INTERIM / "eb1911_dump.sqlite"
ARTICLE_PREFIX = "1911 Encyclopædia Britannica/"
_CONTAINER = re.compile(r"/Vol(ume)?[ _]\d+", re.IGNORECASE)
_SCAN_TITLE = re.compile(r"^Page:EB1911 - Volume (\d+)\.djvu/(\d+)$")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=120)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS runs (
            idx INTEGER PRIMARY KEY, start INTEGER, end INTEGER, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS stubs (title TEXT PRIMARY KEY, wikitext TEXT);
        CREATE TABLE IF NOT EXISTS scan (
            volume INTEGER, page INTEGER, wikitext TEXT, PRIMARY KEY (volume, page));
    """)
    return con


def wanted(title: str) -> bool:
    """An EB1911 article, or one of the scan pages articles are built from."""
    if title.startswith(ARTICLE_PREFIX):
        return not _CONTAINER.search(title)
    return bool(_SCAN_TITLE.match(title))


def build_plan(con: sqlite3.Connection, index_path, dump_size: int,
               merge_gap: int) -> None:
    """Work out and record the byte ranges holding the 1911 layer."""
    plan = ms.build_plan(index_path, wanted, dump_size, merge_gap)
    log.info("plan: %s", plan.summary())
    con.execute("DELETE FROM runs")
    con.executemany("INSERT INTO runs (idx, start, end) VALUES (?,?,?)",
                    [(i, a, b) for i, (a, b) in enumerate(plan.runs)])
    con.execute("INSERT OR REPLACE INTO meta VALUES ('dump_size', ?)", (str(dump_size),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('planned', ?)", (str(time.time()),))
    con.commit()


def store_pages(con: sqlite3.Connection, xml_text: str) -> tuple[int, int]:
    """Keep the EB1911 articles and scan pages out of one decompressed chunk."""
    arts, scans = [], []
    for title, body in ms.iter_pages(xml_text):
        m = _SCAN_TITLE.match(title)
        if m:
            scans.append((int(m.group(1)), int(m.group(2)), body))
        elif title.startswith(ARTICLE_PREFIX) and not _CONTAINER.search(title):
            arts.append((title, body))
    if arts:
        con.executemany("INSERT OR REPLACE INTO stubs VALUES (?,?)", arts)
    if scans:
        con.executemany("INSERT OR REPLACE INTO scan VALUES (?,?,?)", scans)
    return len(arts), len(scans)


def fetch_ranges(con: sqlite3.Connection, sess: requests.Session, per_minute: float,
                 limit: int = 0) -> None:
    """Pull every planned byte range, decompress it, keep what matters.

    A range covers whole bzip2 streams, and `bz2.decompress` handles a
    concatenation of them, so each range decompresses in one call. The result is
    a bare run of `<page>` elements with no document root, which is why they are
    matched out individually rather than parsed as a document.
    """
    q = "SELECT idx, start, end FROM runs WHERE done = 0 ORDER BY idx"
    todo = con.execute(q + (" LIMIT ?" if limit else ""),
                       (limit,) if limit else ()).fetchall()
    done_already = con.execute("SELECT COUNT(*) FROM runs WHERE done = 1").fetchone()[0]
    if not todo:
        log.info("all %d ranges already fetched", done_already)
        return
    log.info("fetching %d ranges (%d already done)", len(todo), done_already)

    url = DUMP_BASE + MULTISTREAM_FILE
    lim = RateLimiter(per_minute)
    got_a = got_s = 0
    t0 = time.time()
    for n, (idx, start, end) in enumerate(todo, 1):
        lim.wait()
        try:
            xml_text = ms.fetch_range(sess, url, start, end)
        except OSError as e:  # a truncated or misaligned stream
            log.warning("range %d (%d-%d) did not decompress: %s", idx, start, end, e)
            continue
        except RuntimeError as e:
            raise RuntimeError(f"{e}; re-run with --full to stream the whole dump") from e
        a, s = store_pages(con, xml_text)
        got_a += a
        got_s += s
        con.execute("UPDATE runs SET done = 1 WHERE idx = ?", (idx,))
        if n % 50 == 0 or n == len(todo):
            con.commit()
            rate = n / max(time.time() - t0, 1e-6)
            log.info("%d/%d ranges, %d articles, %d scan pages, %.1f ranges/s",
                     n, len(todo), got_a, got_s, rate)
    con.commit()


def fetch_full(con: sqlite3.Connection, sess: requests.Session) -> None:
    """Stream the ordinary dump end to end: one request, four times the bytes."""
    url = DUMP_BASE + FULL_FILE
    log.info("streaming %s (%.2f GB)", FULL_FILE, ms.remote_size(sess, url) / 1e9)
    dec = bz2.BZ2Decompressor()
    buf = ""
    got_a = got_s = 0
    seen = 0
    with sess.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 22):
            seen += len(chunk)
            buf += dec.decompress(chunk).decode("utf-8", "replace")
            cut = buf.rfind("</page>")
            if cut == -1:
                continue
            a, s = store_pages(con, buf[:cut + 7])
            got_a += a
            got_s += s
            buf = buf[cut + 7:]
            con.commit()
            log.info("%.2f GB read, %d articles, %d scan pages", seen / 1e9, got_a, got_s)
    a, s = store_pages(con, buf)
    con.commit()
    log.info("done: %d articles, %d scan pages", got_a + a, got_s + s)


def resolve(con: sqlite3.Connection) -> None:
    """Reconstruct every article from its scan pages and write the shared store."""
    out = eb1911_connect()
    # Preserve whatever the API route fetched before overwriting those titles,
    # so `--phases validate` has something to compare against.
    kept = out.execute("""INSERT OR REPLACE INTO articles_api
                          SELECT * FROM articles WHERE source = 'api'""").rowcount
    out.commit()
    if kept > 0:
        log.info("kept %d API articles for comparison", kept)

    pages: dict[int, dict[int, str]] = {}
    for vol, page, body in con.execute("SELECT volume, page, wikitext FROM scan"):
        pages.setdefault(vol, {})[page] = body
    log.info("scan pages loaded: %d volumes, %d pages",
             len(pages), sum(len(v) for v in pages.values()))

    stats = {"resolved": 0, "no_transclusion": 0, "missing_volume": 0,
             "incomplete": 0, "empty": 0}
    rows = []
    for title, stub in con.execute("SELECT title, wikitext FROM stubs"):
        spec = wt.parse_pages_tag(stub or "")
        if not spec:
            stats["no_transclusion"] += 1
            continue
        vol_pages = pages.get(spec["volume"])
        if not vol_pages:
            stats["missing_volume"] += 1
            continue
        text, missing = wt.assemble(spec, vol_pages)
        if missing:
            stats["incomplete"] += 1
        if not text:
            stats["empty"] += 1
            continue
        rows.append((title, title.split("/", 1)[-1], str(spec["volume"]),
                     wt.header_param(stub, "wikipedia"),
                     text, len(text), time.time(), "dump"))
        stats["resolved"] += 1
        if len(rows) >= 2000:
            out.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?)", rows)
            out.commit()
            rows = []
    if rows:
        out.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?)", rows)
    out.commit()

    total_chars = out.execute(
        "SELECT SUM(chars) FROM articles WHERE source = 'dump'").fetchone()[0] or 0
    log.info("resolved %d articles (%.0f MB of prose); "
             "%d carry no transclusion, %d name a volume not in the store, "
             "%d are missing at least one scan page, %d came out empty",
             stats["resolved"], total_chars / 1e6, stats["no_transclusion"],
             stats["missing_volume"], stats["incomplete"], stats["empty"])


def validate() -> None:
    """Compare dump reconstructions against the API renderings of the same pages.

    The API rows are ground truth: they are what MediaWiki itself produces from
    the same transclusion. Agreement on length and vocabulary is the evidence
    that the offline reconstruction is doing the same job.
    """
    con = eb1911_connect()
    api = {t: x for t, x in con.execute("SELECT title, text FROM articles_api")}
    api.update({t: x for t, x in con.execute(
        "SELECT title, text FROM articles WHERE source = 'api'")})
    dump = {t: x for t, x in con.execute(
        "SELECT title, text FROM articles WHERE source = 'dump'")}
    common = sorted(set(api) & set(dump))
    if not common:
        log.info("nothing to validate: no title has been fetched by both routes")
        return
    ratios, jaccs = [], []
    for t in common:
        a, d = api[t], dump[t]
        ratios.append(len(d) / max(len(a), 1))
        ta, td = set(a.lower().split()), set(d.lower().split())
        jaccs.append(len(ta & td) / max(len(ta | td), 1))
    log.info("validated %d articles present in both routes", len(common))
    log.info("length ratio dump/api: median %.2f, 10th pct %.2f, 90th pct %.2f",
             statistics.median(ratios), _pct(ratios, 10), _pct(ratios, 90))
    log.info("token overlap (Jaccard): median %.2f, 10th pct %.2f",
             statistics.median(jaccs), _pct(jaccs, 10))

    # The API rendering carries page furniture that the transclusion does not:
    # breadcrumbs, previous and next headwords, "Wikidata item", the project
    # disclaimer. That is a fixed overhead per article, so it swamps a short
    # entry and disappears into a long one. If that is what the disagreement is,
    # agreement has to rise with length, and this is the check.
    bands = [(0, 1000), (1000, 5000), (5000, 20000), (20000, 10 ** 9)]
    for lo, hi in bands:
        sel = [(j, r) for j, r, t in zip(jaccs, ratios, common) if lo <= len(api[t]) < hi]
        if sel:
            log.info("  api length %6d-%-8s n=%-4d  Jaccard %.2f  length ratio %.2f",
                     lo, (str(hi) if hi < 10 ** 9 else "up"), len(sel),
                     statistics.median([j for j, _ in sel]),
                     statistics.median([r for _, r in sel]))
    worst = sorted(zip(jaccs, common))[:5]
    for j, t in worst:
        log.info("  weakest agreement %.2f  %s", j, t)


def _pct(xs, p):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, int(len(xs) * p / 100)))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phases", default="index,plan,fetch,resolve",
                    help="comma separated: index, plan, fetch, resolve, validate")
    ap.add_argument("--full", action="store_true",
                    help="stream the whole 3.4 GB dump instead of byte ranges")
    ap.add_argument("--merge-gap", type=int, default=256 * 1024,
                    help="merge byte ranges separated by less than this many bytes")
    ap.add_argument("--per-minute", type=float, default=240,
                    help="range requests per minute")
    ap.add_argument("--limit-ranges", type=int, default=0,
                    help="stop after N ranges, for a smoke test")
    args = ap.parse_args()
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}

    con = connect()
    sess = session()

    if args.full:
        fetch_full(con, sess)
        resolve(con)
        return 0

    index_path = RAW / INDEX_FILE
    if "index" in phases:
        ms.ensure_file(sess, DUMP_BASE + INDEX_FILE, index_path, log)

    if "plan" in phases:
        size = ms.remote_size(sess, DUMP_BASE + MULTISTREAM_FILE)
        build_plan(con, index_path, size, args.merge_gap)

    if "fetch" in phases:
        fetch_ranges(con, sess, args.per_minute, args.limit_ranges)

    if "resolve" in phases:
        resolve(con)
    if "validate" in phases:
        validate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

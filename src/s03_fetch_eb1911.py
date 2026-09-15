"""Harvest the 1911 layer from Wikisource, and build the lexicon from it.

Two things make this stage worth its own file.

First, the article body is not in the article. Every EB1911 page is a short
`{{EB1911}}` header plus a `<pages index="EB1911 - Volume NN.djvu" .../>`
transclusion of the proofread Page: namespace, so `prop=extracts` returns an
empty string and only `action=parse` renders the text. One call per article with
`prop=text|wikitext` gets the body and the metadata together.

Second, the header carries `wikipedia=`, the editors' own mapping from a 1911
article to a modern Wikipedia title (Gravitation -> Gravity). That is a
hand-curated alignment across a century, free, for most of the corpus, and it is
what s06 links the early editions onto.

The tokens of this layer also become the lexicon that s05 uses to undo the long
s in the eighteenth-century volumes: period vocabulary, already proofread.
"""
from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import ocr
from wikitext import header_param as ocr_header
from common import (EB1911_DB, EB1911_PREFIX, INTERIM, RateLimiter,
                    WIKISOURCE_API, api_get, eb1911_connect, get_logger, session)

log = get_logger("s03")
DB = EB1911_DB
LEXICON = INTERIM / "lexicon.tsv"
TITLES = INTERIM / "eb1911_titles.txt"

# Volume front matter, not articles.
_CONTAINER = re.compile(r"/Vol(ume)?[ _]\d+", re.IGNORECASE)
_STYLE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_NAV = re.compile(r"^(\u2190|\u2192|Sister projects|This article was|\[\d+\])", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def connect() -> sqlite3.Connection:
    return eb1911_connect()


def list_titles(sess, refresh: bool = False) -> list[str]:
    """Every mainspace page under the EB1911 prefix, container pages removed.

    Enumerating 37k titles through `apprefix` takes minutes on its own, so the
    list is cached; it changes only when Wikisource proofreads new articles.
    """
    if TITLES.exists() and not refresh:
        return TITLES.read_text(encoding="utf-8").splitlines()
    titles, cont = [], {}
    lim = RateLimiter(120)
    while True:
        js = api_get(sess, WIKISOURCE_API, {
            "action": "query", "list": "allpages", "apprefix": EB1911_PREFIX,
            "apnamespace": 0, "aplimit": 500, **cont}, limiter=lim)
        titles += [p["title"] for p in js["query"]["allpages"]]
        if "continue" not in js:
            break
        cont = js["continue"]
        if len(titles) % 5000 < 500:
            log.info("enumerated %d titles", len(titles))
    titles = [t for t in titles if not _CONTAINER.search(t)]
    TITLES.write_text("\n".join(titles), encoding="utf-8")
    log.info("%d article titles cached -> %s", len(titles), TITLES)
    return titles


def clean_html(raw: str) -> str:
    txt = html.unescape(_TAG.sub("\n", _STYLE.sub("", raw)))
    lines = [ln.strip() for ln in txt.splitlines()]
    keep = [ln for ln in lines if ln and not _NAV.match(ln)]
    return "\n".join(keep)


def fetch_one(sess, lim: RateLimiter, title: str) -> dict | None:
    try:
        js = api_get(sess, WIKISOURCE_API, {
            "action": "parse", "page": title, "prop": "text|wikitext"}, limiter=lim)
    except Exception as e:  # noqa: BLE001
        log.warning("%s: %s", title, e)
        return None
    parse = js.get("parse", {})
    wt = parse.get("wikitext", "")
    text = clean_html(parse.get("text", ""))
    return {
        "title": title,
        "headword": title.split("/", 1)[-1],
        "volume": ocr_header(wt, "volume"),
        "wikipedia": ocr_header(wt, "wikipedia"),
        "text": text, "chars": len(text), "fetched": time.time(),
    }


def build_lexicon(con: sqlite3.Connection, min_count: int = 2) -> int:
    """Token counts over the 1911 layer, the reference vocabulary for s05."""
    counts: Counter[str] = Counter()
    for (text,) in con.execute("SELECT text FROM articles"):
        # Tokenise exactly the way s05 will when it measures against this
        # lexicon. They used to differ by one character class, which put every
        # single-letter word permanently out of vocabulary and quietly added
        # about two and a half points to every OOV rate in the project.
        counts.update(ocr.tokens(text or ""))
    kept = {w: c for w, c in counts.items() if c >= min_count}
    with LEXICON.open("w", encoding="utf-8") as fh:
        for w, c in sorted(kept.items(), key=lambda kv: -kv[1]):
            fh.write(f"{w}\t{c}\n")
    log.info("lexicon: %d types kept of %d seen (min_count=%d) -> %s",
             len(kept), len(counts), min_count, LEXICON)
    return len(kept)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="stop after N new articles")
    # See s04 for the 429 behaviour; the same applies here, on top of the
    # cold-render cost of a transcluded page.
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--per-minute", type=float, default=120)
    ap.add_argument("--lexicon-only", action="store_true")
    ap.add_argument("--refresh-titles", action="store_true")
    args = ap.parse_args()

    con = connect()
    if args.lexicon_only:
        build_lexicon(con)
        return 0

    sess = session()
    have = {t for (t,) in con.execute("SELECT title FROM articles")}
    titles = [t for t in list_titles(sess, refresh=args.refresh_titles) if t not in have]
    log.info("%d articles already stored, %d to fetch", len(have), len(titles))
    if args.limit:
        titles = titles[:args.limit]

    lim = RateLimiter(args.per_minute)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for rec in pool.map(lambda t: fetch_one(sess, lim, t), titles):
            if not rec:
                continue
            con.execute(
                "INSERT OR REPLACE INTO articles VALUES (:title,:headword,:volume,"
                ":wikipedia,:text,:chars,:fetched,'api')", rec)
            done += 1
            if done % 200 == 0:
                con.commit()
                log.info("%d/%d fetched", done, len(titles))
    con.commit()

    n, aligned, chars = con.execute(
        "SELECT COUNT(*), SUM(wikipedia IS NOT NULL), SUM(chars) FROM articles").fetchone()
    log.info("stored %d articles, %d (%.0f%%) carry a wikipedia= alignment, %.1f MB of text",
             n, aligned or 0, 100 * (aligned or 0) / max(n, 1), (chars or 0) / 1e6)
    build_lexicon(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Era-locked indexes, and the measurement of what 250 years of drift costs.

One index per era, and the sealing is structural rather than a promise: each era
gets its own FTS5 table, so a query against 1771 cannot reach an 1860 passage
because there is nowhere for it to look. Nothing falls back to a later era when
an earlier one retrieves badly, because retrieving badly is the finding.

SQLite's FTS5 provides the BM25 ranking, which means the whole index is a file
and needs no dependency beyond the standard library. Each entry is stored twice,
once as the scanner read it and once normalised, so the same query can be run
against both and the difference read off. That difference is the number this
project exists to produce: **how much retrieval quality does eighteenth-century
typography cost, and how much of it can be given back?**

## The query set

Hand-labelling gold entries across twelve eras is not something this project can
honestly claim to have done, so the queries are built from the corpus instead.
For every subject that `s06` linked across at least eight eras and that has a
modern article, the query is the opening of that modern article **with the
subject's own name removed**, and the gold answer is whichever entry the linker
assigned to that subject in each era.

Removing the name is what makes the task real. Left in, a query for cholera
contains the word "cholera", every era's entry is headed CHOLERA, headwords
survive OCR almost perfectly, and the measurement would be of headword matching
rather than of reading the text. Removed, retrieval has to work from the
description: *an infection of the small intestine caused by some strains of the
bacterium...* against whatever that era wrote.

The labels come from the linker, which uses headwords only and never reads a
word of the body text, so they are independent of what is being measured here.
They are not independent of the linker's own mistakes, which the linking audit
put at a few per cent on the weak routes.

## What is not here

The dense and hybrid arms of the ablation are specified in notes/DESIGN.md and
not built. Encoding roughly 800,000 chunks with a sentence transformer on CPU is
hours of work for an arm whose result is largely predictable: embedding models
have seen very little eighteenth-century orthography and none of this scanner's
failure modes. BM25 over normalised text is the baseline to beat and, for the
early eras, probably the ceiling.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
import statistics
import sys
from collections import defaultdict

from common import ERAS, EB1911_DB, INTERIM, PROC, TABLES, get_logger

log = get_logger("s07")

CHUNKS_DB = INTERIM / "chunks.sqlite"
MODERN_DB = INTERIM / "modern.sqlite"
SUBJECTS = PROC / "subjects.jsonl"
QUERIES = INTERIM / "queries.jsonl"

VARIANTS = ("norm", "raw")
MAX_CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
MIN_ERAS_FOR_QUERY = 8
QUERY_WORDS = 45
TOP_K = 10

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")
_PARA = re.compile(r"\n\s*\n")
# Everything here is either a function word or an artefact of the genre: every
# modern article opens "X is a ...", every early entry opens "X, in botany, a ...".
STOPWORDS = set("""
a an the of and or in on at to for from by with without within into onto upon as
is are was were be been being am has have had do does did not no nor but if then
than that this these those it its he she they them his her their our your my me
we you i which who whom whose what when where why how all any both each few more
most other some such only own same so too very can will just don should now also
one two three first second new old called known named term name given used use
also may might must shall would could there here about after before between
during under over again further once any many much
""".split())


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(CHUNKS_DB, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS chunks (
        chunk_id INTEGER PRIMARY KEY, era TEXT, variant TEXT, doc_id TEXT,
        headword TEXT, seq INTEGER, text TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_chunks_era ON chunks(era, variant)")
    return con


def fts_table(era: str, variant: str) -> str:
    return f"fts_{era}_{variant}"


def split_text(text: str, limit: int = MAX_CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Cut a long entry into overlapping windows on paragraph boundaries.

    Most entries in this corpus are two lines long and pass through untouched.
    The exceptions are the systematic treatises, where *Chemistry* runs to dozens
    of printed pages, and modern Wikipedia articles, which average 15 KB. A whole
    treatise is useless as a retrieval unit.
    """
    if len(text) <= limit:
        return [text]
    def from_word_start(s: str) -> str:
        """Drop a leading half-word, so an overlap does not begin mid-token."""
        space = s.find(" ")
        return s[space + 1:] if 0 <= space < 40 else s

    def tail(s: str, n: int) -> str:
        return from_word_start(s[-n:])

    paras = [p.strip() for p in _PARA.split(text) if p.strip()] or [text]
    windows, cur = [], ""
    for para in paras:
        while len(para) > limit:  # a single paragraph longer than the window
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            windows.append(para[:cut])
            para = from_word_start(para[max(0, cut - overlap):])
        if not cur:
            cur = para
        elif len(cur) + len(para) + 1 <= limit:
            cur = f"{cur}\n{para}"
        else:
            windows.append(cur)
            cur = f"{cur[-overlap:]}\n{para}" if overlap else para
    if cur:
        windows.append(cur)
    return windows


def chunk_rows(era: str, variant: str, doc_id: str, headword: str, text: str):
    """Chunks for one document, each prefixed with the headword.

    The prefix is there so a retrieved fragment from the middle of a treatise
    still says what it is an entry about, which matters as much for the answer
    stage as for scoring.
    """
    for seq, window in enumerate(split_text(text)):
        body = window if window.lstrip().upper().startswith(headword.upper()[:12]) \
            else f"{headword}. {window}"
        yield (era, variant, doc_id, headword, seq, body)


def load_nls(con: sqlite3.Connection) -> None:
    files = sorted(PROC.glob("entries_*.jsonl"))
    if not files:
        raise SystemExit("no segmented entries: run s05 first")
    for path in files:
        era = path.stem.split("_", 1)[1]
        rows, n_docs, missing_raw = [], 0, 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                e = json.loads(line)
                doc_id = f"{era}:{e['volume_id']}:{e['entry_index']}"
                n_docs += 1
                rows += list(chunk_rows(era, "norm", doc_id, e["headword"], e["text"]))
                raw = e.get("raw")
                if raw:
                    rows += list(chunk_rows(era, "raw", doc_id, e["headword"], raw))
                else:
                    missing_raw += 1
                if len(rows) >= 20000:
                    insert(con, rows)
                    rows = []
        insert(con, rows)
        note = f", {missing_raw} without a raw variant (re-run s05)" if missing_raw else ""
        log.info("%s: %d entries chunked%s", era, n_docs, note)


def load_1911(con: sqlite3.Connection) -> None:
    src = sqlite3.connect(EB1911_DB)
    rows, n = [], 0
    for headword, text in src.execute(
            "SELECT headword, text FROM articles WHERE text IS NOT NULL AND text != ''"):
        n += 1
        rows += list(chunk_rows("1911", "norm", f"1911:{headword}", headword, text))
        if len(rows) >= 20000:
            insert(con, rows)
            rows = []
    insert(con, rows)
    log.info("1911: %d articles chunked", n)


def load_modern(con: sqlite3.Connection) -> None:
    if not MODERN_DB.exists():
        log.warning("no modern layer: run s04b")
        return
    src = sqlite3.connect(MODERN_DB)
    rows, n = [], 0
    for title, text in src.execute(
            "SELECT title, text FROM pages WHERE missing = 0 AND text != ''"):
        n += 1
        rows += list(chunk_rows("modern", "norm", f"modern:{title}", title, text))
        if len(rows) >= 20000:
            insert(con, rows)
            rows = []
    insert(con, rows)
    log.info("modern: %d articles chunked", n)


def insert(con: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    con.executemany("""INSERT INTO chunks (era, variant, doc_id, headword, seq, text)
                       VALUES (?,?,?,?,?,?)""", rows)
    con.commit()


def build_indexes(con: sqlite3.Connection) -> None:
    """One FTS5 table per era and variant. The separation is the era locking."""
    pairs = con.execute(
        "SELECT era, variant, COUNT(*) FROM chunks GROUP BY era, variant").fetchall()
    for era, variant, n in pairs:
        name = fts_table(era, variant)
        con.execute(f"DROP TABLE IF EXISTS {name}")
        # An external content table keeps the text stored once rather than twice.
        con.execute(f"""CREATE VIRTUAL TABLE {name} USING fts5(
            text, content='chunks', content_rowid='chunk_id')""")
        con.execute(f"""INSERT INTO {name}(rowid, text)
            SELECT chunk_id, text FROM chunks WHERE era = ? AND variant = ?""",
                    (era, variant))
        con.commit()
        log.info("%-18s %7d chunks indexed", name, n)


def fts_query(text: str, drop: set[str], limit: int = 25) -> str:
    """Turn a sentence into an FTS5 OR query, dropping the subject's own name."""
    terms, seen = [], set()
    for m in _WORD.finditer(text.lower()):
        w = m.group(0)
        if w in STOPWORDS or w in drop or len(w) < 3 or w in seen:
            continue
        seen.add(w)
        terms.append('"' + w.replace('"', "") + '"')
        if len(terms) >= limit:
            break
    return " OR ".join(terms)


def build_queries(seed: int, n_queries: int) -> list[dict]:
    """Queries from modern article openings; gold answers from the linker."""
    if not SUBJECTS.exists():
        raise SystemExit("no subjects: run s06 first")
    modern_text: dict[str, str] = {}
    if MODERN_DB.exists():
        src = sqlite3.connect(MODERN_DB)
        for title, text in src.execute(
                "SELECT title, text FROM pages WHERE missing = 0 AND text != ''"):
            modern_text[f"modern:{title}"] = text

    pool = []
    with SUBJECTS.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["n_eras"] < MIN_ERAS_FOR_QUERY:
                continue
            gold: dict[str, list[str]] = defaultdict(list)
            modern_ids, names = [], set()
            for m in r["members"]:
                gold[m["era"]] += m["entries"]
                names.add(m["headword"].lower())
                if m["era"] == "modern":
                    modern_ids += m["entries"]
            source = next((modern_text[i] for i in modern_ids if i in modern_text), None)
            if not source:
                continue
            drop = {w for name in names for w in _WORD.findall(name.lower())}
            words = source.split()[:QUERY_WORDS]
            q = fts_query(" ".join(words), drop)
            if q.count("OR") < 4:  # too thin to be a question about anything
                continue
            pool.append({"subject_id": r["subject_id"], "label": r["label"],
                         "query": q, "source": " ".join(words),
                         "gold": {k: sorted(set(v)) for k, v in gold.items()}})
    random.Random(seed).shuffle(pool)
    out = pool[:n_queries]
    with QUERIES.open("w", encoding="utf-8") as fh:
        for q in out:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")
    log.info("%d candidate subjects, %d queries written -> %s",
             len(pool), len(out), QUERIES)
    return out


def search(con: sqlite3.Connection, era: str, variant: str, query: str,
           k: int = TOP_K) -> list[str]:
    """Top-k doc_ids for one query in one era. Nothing crosses the era boundary."""
    name = fts_table(era, variant)
    try:
        rows = con.execute(f"""
            SELECT c.doc_id FROM {name} f JOIN chunks c ON c.chunk_id = f.rowid
            WHERE {name} MATCH ? ORDER BY bm25({name}) LIMIT ?""",
                           (query, k * 3)).fetchall()
    except sqlite3.OperationalError:
        return []
    out, seen = [], set()
    for (doc_id,) in rows:
        if doc_id not in seen:
            seen.add(doc_id)
            out.append(doc_id)
        if len(out) >= k:
            break
    return out


def evaluate(con: sqlite3.Connection, queries: list[dict]) -> None:
    have = {(e, v) for e, v in con.execute(
        "SELECT DISTINCT era, variant FROM chunks")}
    results: dict[tuple[str, str], dict] = {}
    for era in ERAS:
        for variant in VARIANTS:
            if (era, variant) not in have:
                continue
            hits1 = hits5 = hits10 = asked = 0
            rr = []
            for q in queries:
                gold = set(q["gold"].get(era, ()))
                if not gold:
                    continue
                asked += 1
                ranked = search(con, era, variant, q["query"])
                pos = next((i for i, d in enumerate(ranked, 1) if d in gold), 0)
                if pos:
                    rr.append(1.0 / pos)
                    hits10 += 1
                    hits5 += pos <= 5
                    hits1 += pos == 1
                else:
                    rr.append(0.0)
            if not asked:
                continue
            results[(era, variant)] = {
                "era": era, "variant": variant, "queries": asked,
                "recall_at_1": round(hits1 / asked, 3),
                "recall_at_5": round(hits5 / asked, 3),
                "recall_at_10": round(hits10 / asked, 3),
                "mrr": round(statistics.fmean(rr), 3),
            }
            log.info("%-7s %-5s n=%-4d r@1 %.2f  r@5 %.2f  r@10 %.2f  mrr %.2f",
                     era, variant, asked, results[(era, variant)]["recall_at_1"],
                     results[(era, variant)]["recall_at_5"],
                     results[(era, variant)]["recall_at_10"],
                     results[(era, variant)]["mrr"])

    out = TABLES / "retrieval_ablation.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["era", "variant", "queries", "recall_at_1",
                                           "recall_at_5", "recall_at_10", "mrr"])
        w.writeheader()
        for era in ERAS:
            for variant in VARIANTS:
                if (era, variant) in results:
                    w.writerow(results[(era, variant)])
    log.info("ablation -> %s", out)

    gains = [(e, results[(e, "norm")]["recall_at_10"] - results[(e, "raw")]["recall_at_10"])
             for e in ERAS if (e, "norm") in results and (e, "raw") in results]
    for era, gain in gains:
        log.info("  normalisation gain at %s: %+.3f recall@10", era, gain)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phases", default="chunk,index,queries,eval")
    ap.add_argument("--queries-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}

    con = connect()
    if "chunk" in phases:
        con.execute("DELETE FROM chunks")
        con.commit()
        load_nls(con)
        load_1911(con)
        load_modern(con)
        total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        log.info("%d chunks in %s", total, CHUNKS_DB)
    if "index" in phases:
        build_indexes(con)

    queries: list[dict] = []
    if "queries" in phases:
        queries = build_queries(args.seed, args.queries_n)
    elif QUERIES.exists():
        queries = [json.loads(l) for l in QUERIES.open(encoding="utf-8")]

    if "eval" in phases:
        if not queries:
            raise SystemExit("no queries: run the queries phase")
        evaluate(con, queries)
    return 0


if __name__ == "__main__":
    sys.exit(main())

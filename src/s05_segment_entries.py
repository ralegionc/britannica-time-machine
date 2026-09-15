"""Cut volume text into entries, and normalise each entry's prose.

The early volumes are one plain text file per volume with no article structure
at all, so the entries have to be recovered from typography that survived OCR.
What survived is the headword: entries begin at line start in full capitals
followed by punctuation, and because the long s is a lowercase letter, capitalised
headwords come through the scan clean (ADHESION, not ADHEFION) even where the
body text does not. That asymmetry is what makes this tractable.

Order matters. Dehyphenation runs first because entry boundaries are line-based
and a word split across a line break can hide a headword. Long-s restoration and
reflowing run last, per entry, because both destroy line structure.

Everything rejected is counted, not silently dropped: page furniture, running
heads, too-short bodies and roman-numeral false positives all appear in
outputs/tables/segmentation_stats.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter

import ocr
from common import INTERIM, PROC, TABLES, VOLUMES, get_logger

log = get_logger("s05")

HEADWORD = re.compile(r"^(?P<hw>[A-Z\u00c6][A-Z\u00c6'\-]{2,})\s*(?P<punct>[,.;:])")
ROMAN = re.compile(r"^[IVXLCDM]+$")
VOWEL = re.compile(r"[AEIOUY\u00c6]")
# "ADM (21", "6 ADM", bare folio numbers, and plate captions between entries.
FURNITURE = re.compile(r"^(\d+\s*)?[A-Z]{2,5}\s*[\(\[]?\s*\d|^\d{1,4}$|^[\W\d]{0,4}$")
MIN_BODY_CHARS = 40


def load_lexicon() -> Counter:
    path = INTERIM / "lexicon.tsv"
    if not path.exists():
        raise SystemExit("no lexicon: run s03 first (it builds one from the 1911 layer)")
    lex: Counter[str] = Counter()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            w, _, c = line.rstrip("\n").partition("\t")
            lex[w] = int(c or 1)
    return lex


def is_headword(hw: str) -> bool:
    return len(hw) >= 3 and not ROMAN.match(hw) and bool(VOWEL.search(hw))


def segment(text: str, lexicon) -> tuple[list[dict], Counter]:
    """Split one volume into entries. Returns entries and a rejection tally."""
    stats: Counter[str] = Counter()
    lines = ocr.dehyphenate(text, lexicon).splitlines()
    entries: list[dict] = []
    cur: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if FURNITURE.match(line):
            stats["furniture_lines"] += 1
            continue
        m = HEADWORD.match(line)
        if m and is_headword(m.group("hw")):
            if cur:
                entries.append(cur)
            cur = {"headword": m.group("hw"), "lines": [line]}
            stats["headwords_seen"] += 1
            continue
        if m:
            stats["headword_rejected"] += 1
        if cur:
            cur["lines"].append(line)
        else:
            stats["preamble_lines"] += 1
    if cur:
        entries.append(cur)

    kept = []
    for e in entries:
        body = "\n".join(e["lines"])
        if len(body) < MIN_BODY_CHARS:
            stats["too_short"] += 1
            continue
        kept.append({"headword": e["headword"], "raw": body})
    stats["entries"] = len(kept)
    return kept, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-oov", type=int, default=200,
                    help="entries per volume measured for out-of-vocabulary rate")
    args = ap.parse_args()

    lexicon = load_lexicon()
    log.info("lexicon: %d types", len(lexicon))

    meta = {r["volume_id"]: r for r in csv.DictReader(
        (INTERIM / "volumes.csv").open(encoding="utf-8"))}
    files = sorted(VOLUMES.glob("*.txt"))
    if not files:
        raise SystemExit("no volumes on disk: run s02 first")

    writers: dict[str, object] = {}
    rows = []
    try:
        for path in files:
            vid = path.stem
            m = meta.get(vid, {})
            era = m.get("year", "unknown")
            text = path.read_text(encoding="utf-8", errors="replace")
            entries, stats = segment(text, lexicon)

            # Evenly spaced, not the first N: the opening entries of a volume
            # sit next to title pages, prefaces and handwritten flyleaf
            # marginalia, and measuring there overstates the noise by a factor
            # of two against the body of the book.
            stride = max(1, len(entries) // max(args.sample_oov, 1))
            sample = entries[::stride][:args.sample_oov]
            sample_idx = set(range(0, len(entries), stride))
            before_bad = before_tot = 0
            for e in sample:
                b, t = ocr.oov_counts(e["raw"], lexicon)
                before_bad += b
                before_tot += t

            if era not in writers:
                writers[era] = (PROC / f"entries_{era}.jsonl").open("w", encoding="utf-8")
            fh = writers[era]
            after_bad = after_tot = 0
            for i, e in enumerate(entries):
                norm = ocr.normalise(e["raw"], lexicon)
                if i in sample_idx:
                    b, t = ocr.oov_counts(norm, lexicon)
                    after_bad += b
                    after_tot += t
                fh.write(json.dumps({
                    "era": era, "edition_no": m.get("edition_no"),
                    "volume_id": vid, "volume": m.get("volume"), "span": m.get("span"),
                    "entry_index": i, "headword": e["headword"],
                    "text": norm, "n_chars": len(norm),
                }, ensure_ascii=False) + "\n")
            oov_before = before_bad / max(before_tot, 1)
            oov_after = after_bad / max(after_tot, 1)

            rows.append({
                "volume_id": vid, "era": era, "edition_no": m.get("edition_no"),
                "volume": m.get("volume"), "span": m.get("span"),
                "entries": stats["entries"], "headwords_seen": stats["headwords_seen"],
                "headword_rejected": stats["headword_rejected"],
                "furniture_lines": stats["furniture_lines"],
                "too_short": stats["too_short"],
                "oov_before": round(oov_before, 4), "oov_after": round(oov_after, 4),
            })
            log.info("%s era %s: %5d entries, OOV %.1f%% -> %.1f%%", vid, era,
                     stats["entries"], 100 * oov_before, 100 * oov_after)
    finally:
        for fh in writers.values():
            fh.close()

    out = TABLES / "segmentation_stats.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("%d volumes, %d entries -> %s", len(rows),
             sum(r["entries"] for r in rows), PROC)
    log.info("stats -> %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

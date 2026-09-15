"""What changed between the answers, which is the output the project is for.

`s08` produces a row of answers per question, one per era. On its own that is a
transcript. The findings are in the differences, and there are four kinds worth
measuring, each of which needs the twelve-layer corpus and none of which needs a
model:

**When a word entered the encyclopaedia.** Every question in `eval/questions.yaml`
carries an `expect_shift` field naming the terms the hypothesis turns on
(*miasma*, *contagion*, *Vibrio*). For each of those terms this stage reports the
first era whose corpus contains it at all, and the last. That is a direct test of
the hypothesis the question was written around, and it does not depend on any
answer being good.

**Which words died.** Terms common in the early editions and absent from the late
ones: *phlogiston*, *caloric*, *animalcule*. This is corpus-wide rather than
per-question, and it is the cleanest single artefact the corpus produces, once
one trap is avoided. The first run of this table returned *fame*, *muft*, *fide*,
*cafe*, *feen*, *weft* and *ihould*, which are not words that fell out of use but
long-s readings of *same*, *must*, *side*, *case*, *seen*, *west* and *should*.
They collapse between the fifth edition and the sixth because the long s does,
and without a filter this table simply rediscovers the typography for a third
time. `ocr.is_long_s_suspect` removes them.

**How fast the vocabulary turned over.** Jaccard overlap between the commonest
terms of consecutive eras, which says where the discontinuities are.

**How long the entry was.** A subject's entry length across the eras tracks how
much the compilers thought it mattered.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict

import yaml

import ocr
from common import ERAS, EVAL, INTERIM, PROC, TABLES, get_logger
from s05_segment_entries import load_lexicon
from s08_answer import STOPWORDS, content_words

log = get_logger("s09")

VOCAB_DIR = INTERIM / "vocab"
SUBJECTS = PROC / "subjects.jsonl"

EARLY = ("1771", "1784", "1797", "1801", "1810", "1817")
LATE = ("1842", "1860", "1911", "modern")
TURNOVER_TOP = 5000
DYING_MIN_EARLY = 40
# A candidate must be attested this often in the proofread 1911 layer. Long-s
# filtering alone was not enough: two-error readings such as *fubjeft* (subject),
# *difeovered* (discovered) and *refpeft* (respect) survive it, and they share one
# property that every real archaism lacks, which is that a proofread text never
# contains them. Phlogiston occurs 36 times in the 1911 layer, caloric 23, miasma
# 10, animalcule 4; fubjeft, difeovered and refpeft occur zero times each.
MIN_1911_ATTESTATION = 2


def load_counts(era: str) -> Counter:
    path = VOCAB_DIR / f"{era}.txt.gz"
    if not path.exists():
        raise SystemExit(f"no vocabulary for {era}: run s08 --phases vocab")
    counts: Counter[str] = Counter()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            w, _, c = line.rstrip("\n").partition("\t")
            counts[w] = int(c or 1)
    return counts


def available_eras() -> list[str]:
    return [e for e in ERAS if (VOCAB_DIR / f"{e}.txt.gz").exists()]


def first_and_last_appearance(counts: dict[str, Counter], eras: list[str]) -> None:
    """For every term the question set turns on, when the corpus starts using it."""
    doc = yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8")) or {}
    rows = []
    for q in doc.get("questions", []):
        shift = q.get("expect_shift", "")
        terms = [t for t in dict.fromkeys(content_words(shift)) if len(t) > 3]
        for term in terms:
            present = [e for e in eras if counts[e].get(term)]
            if not present:
                rows.append({"question": q["id"], "term": term, "first_era": "",
                             "last_era": "", "n_eras": 0, "peak_era": "",
                             "peak_per_million": 0.0})
                continue
            per_m = {e: 1e6 * counts[e][term] / max(sum(counts[e].values()), 1)
                     for e in present}
            peak = max(per_m, key=per_m.get)
            rows.append({"question": q["id"], "term": term,
                         "first_era": present[0], "last_era": present[-1],
                         "n_eras": len(present), "peak_era": peak,
                         "peak_per_million": round(per_m[peak], 2)})
    out = TABLES / "term_first_appearance.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("term appearance -> %s (%d terms)", out, len(rows))
    for r in rows:
        if r["first_era"] and r["first_era"] not in ("1771", ""):
            log.info("  %-14s %-14s enters at %s, peaks %s (%.1f per million)",
                     r["question"], r["term"], r["first_era"], r["peak_era"],
                     r["peak_per_million"])


def dying_terms(counts: dict[str, Counter], eras: list[str]) -> None:
    """Words the early editions used freely and the later ones stopped using."""
    early = [e for e in EARLY if e in eras]
    late = [e for e in LATE if e in eras]
    if not early or not late:
        log.warning("need both early and late eras for the dying-term table")
        return
    early_total = Counter()
    for e in early:
        early_total.update(counts[e])
    late_total = Counter()
    for e in late:
        late_total.update(counts[e])
    early_tokens = sum(early_total.values())
    late_tokens = sum(late_total.values())

    lexicon = load_lexicon()
    suspects = 0
    rows = []
    for term, n in early_total.items():
        if n < DYING_MIN_EARLY or len(term) < 4 or term in STOPWORDS:
            continue
        if lexicon.get(term, 0) < MIN_1911_ATTESTATION:
            suspects += 1
            continue
        if ocr.is_long_s_suspect(term, lexicon):
            suspects += 1
            continue
        early_rate = 1e6 * n / early_tokens
        late_rate = 1e6 * late_total.get(term, 0) / late_tokens
        if late_rate * 20 < early_rate:  # fell by more than twenty times
            present = [e for e in eras if counts[e].get(term)]
            # A word does not stop existing, it stops being used, so the span
            # that means anything runs to the last era still using it at a fifth
            # of its peak rate. Presence alone would run every one of these to
            # "modern", where a single antiquarian mention keeps the count above
            # zero and makes the chart say the opposite of the measurement.
            rates = {e: 1e6 * counts[e][term] / max(sum(counts[e].values()), 1)
                     for e in present}
            peak_era = max(rates, key=rates.get) if rates else ""
            floor = 0.2 * rates[peak_era] if rates else 0
            common = [e for e in present if rates[e] >= floor]
            rows.append({"term": term, "early_count": n,
                         "early_per_million": round(early_rate, 2),
                         "late_per_million": round(late_rate, 3),
                         "ratio": round(early_rate / max(late_rate, 1e-9), 1),
                         "attested_1911": lexicon.get(term, 0),
                         "first_era": present[0] if present else "",
                         "last_era": common[-1] if common else "",
                         "peak_era": peak_era})
    rows.sort(key=lambda r: -r["early_per_million"])
    out = TABLES / "dying_terms.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows[:400])
    log.info("dying terms -> %s (%d found, top 400 written; %d rejected as "
             "scanner damage rather than vocabulary)", out, len(rows), suspects)
    log.info("  steepest falls: %s", ", ".join(r["term"] for r in rows[:15]))


def vocabulary_turnover(counts: dict[str, Counter], eras: list[str]) -> None:
    """Jaccard overlap of the commonest terms in consecutive eras."""
    tops = {}
    for e in eras:
        common = [w for w, _ in counts[e].most_common(TURNOVER_TOP * 2)
                  if w not in STOPWORDS and len(w) > 2]
        tops[e] = set(common[:TURNOVER_TOP])
    rows = []
    for a, b in zip(eras, eras[1:]):
        inter = len(tops[a] & tops[b])
        union = len(tops[a] | tops[b])
        rows.append({"from_era": a, "to_era": b,
                     "jaccard": round(inter / max(union, 1), 3),
                     "shared": inter, "only_from": len(tops[a] - tops[b]),
                     "only_to": len(tops[b] - tops[a])})
    out = TABLES / "vocabulary_turnover.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("turnover -> %s", out)
    for r in rows:
        log.info("  %-7s to %-7s jaccard %.3f", r["from_era"], r["to_era"],
                 r["jaccard"])


def entry_lengths(eras: list[str]) -> None:
    """Median entry length per era, and the trajectory for the question subjects."""
    lengths: dict[str, list[int]] = defaultdict(list)
    for path in sorted(PROC.glob("entries_*.jsonl")):
        era = path.stem.split("_", 1)[1]
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                e = json.loads(line)
                lengths[era].append(e["n_chars"])
    rows = []
    for era in eras:
        if not lengths.get(era):
            continue
        vals = lengths[era]
        rows.append({"era": era, "entries": len(vals),
                     "median_chars": int(statistics.median(vals)),
                     "mean_chars": int(statistics.fmean(vals)),
                     "p90_chars": int(sorted(vals)[int(0.9 * len(vals))])})
    out = TABLES / "entry_length.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("entry length -> %s", out)


def question_timeline() -> None:
    """One long table of the answers, which is what s10 renders."""
    rows = []
    for suffix in ("", "_local"):
        path = PROC / f"answers{suffix}.jsonl"
        if not path.exists():
            continue
        gen = "extractive" if not suffix else "local"
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                rows.append({"generator": gen, "question": r["question"],
                             "kind": r["kind"], "era": r["era"], "mode": r["mode"],
                             "coverage": r["coverage"],
                             "n_anachronisms": len(r["anachronisms"]),
                             "anachronisms": " ".join(r["anachronisms"][:8]),
                             "citations": " ".join(r["citations"][:3]),
                             "answer": r["answer"]})
    if not rows:
        log.warning("no answers yet: run s08")
        return
    out = TABLES / "question_timeline.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("timeline -> %s (%d rows)", out, len(rows))

    for gen in sorted({r["generator"] for r in rows}):
        sel = [r for r in rows if r["generator"] == gen]
        answered = [r for r in sel if not r["mode"].startswith("abstain")]
        flagged = sum(1 for r in answered if r["n_anachronisms"])
        log.info("  %-11s %d pairs, %d answered, %d with anachronisms", gen,
                 len(sel), len(answered), flagged)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-corpus", action="store_true",
                    help="only rebuild the answer timeline")
    args = ap.parse_args()

    question_timeline()
    if args.skip_corpus:
        return 0
    eras = available_eras()
    log.info("loading vocabularies for %d eras", len(eras))
    counts = {e: load_counts(e) for e in eras}
    first_and_last_appearance(counts, eras)
    dying_terms(counts, eras)
    vocabulary_turnover(counts, eras)
    entry_lengths(eras)
    return 0


if __name__ == "__main__":
    sys.exit(main())

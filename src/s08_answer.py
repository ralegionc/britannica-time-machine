"""Answer each question once per era, from that era's evidence only.

The retrieval is `s07`'s and the era locking is structural: one index per era,
nowhere to look outside it. What this stage adds is the three things that decide
whether the row of answers is worth reading.

**Abstention is an answer.** Ask 1771 what carries hereditary information and the
correct behaviour is to decline. The edition has entries on GENERATION and on
HEREDITY; neither addresses the question, and a system that produces fluent prose
anyway has failed in the way that matters. Abstention here is decided on term
coverage, the share of the question's content words that appear anywhere in the
retrieved passages, and separately by the generator, which is told to say so.

**Leakage is the central risk.** A model asked to answer as 1771 knows perfectly
well that cholera is bacterial and will be tempted to say so. Three defences, in
increasing order of what they are worth: instruct it not to, keep the answer
extractive so unsupported sentences are visible, and then check.

**The check is an anachronism detector.** Every content word of an answer is
looked up in the vocabulary of that era's own corpus. A 1771 answer containing
*bacterium*, *germ* or *vibrio* is flagged automatically, because those tokens do
not occur anywhere in the 1771 volumes. It is cheap, it is checkable by anyone,
and it is the number that makes the rest of the timeline trustworthy.

Two generators, and the comparison between them is the point rather than a
fallback. `extractive` assembles the answer from the highest-scoring sentences of
the retrieved passages, quoted, with an entry id on each. It cannot hallucinate
and it cannot be anachronistic, by construction, which makes it the control.
`local` runs a small instruction-tuned model on the CPU over the same passages,
and its anachronism rate against the extractive zero is the measurement.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
from collections import Counter

import yaml

from common import ERAS, EVAL, INTERIM, PROC, TABLES, get_logger

log = get_logger("s08")

CHUNKS_DB = INTERIM / "chunks.sqlite"
VOCAB_DIR = INTERIM / "vocab"
ANSWERS = PROC / "answers.jsonl"

TOP_K = 6
MIN_COVERAGE = 0.12
REFUSAL = "NO ANSWER IN THIS EDITION"
MAX_PASSAGE_CHARS = 900
# 0.5B rather than 1.5B: on 16 CPU cores the larger model needs about 90 seconds
# an answer, which is two hours for 120 question-era pairs. The smaller one is
# weaker, which for the one thing being measured here is not a problem at all: a
# weaker model leaks more period-inappropriate vocabulary, and leakage is what the
# anachronism detector exists to count.
DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")
_SENT = re.compile(r"(?<=[.;:!?])\s+")
SKIPPED: Counter[str] = Counter()

# Function words plus the framing verbs every question uses. Kept separate from
# s07's list: this one is about what a *question* contributes, not an article.
STOPWORDS = set("""
a an the of and or in on at to for from by with within into onto upon as is are
was were be been being am has have had do does did not no nor but if then than
that this these those it its he she they them his her their our your my me we
you i which who whom whose what when where why how all any both each few more
most other some such only own same so too very can will just should now also one
two three first second new old called known named term name given used use may
might must shall would could there here about after before between during under
over again further once many much made make come comes lies lie good
""".split())


def connect() -> sqlite3.Connection:
    if not CHUNKS_DB.exists():
        raise SystemExit("no indexes: run s07 first")
    return sqlite3.connect(CHUNKS_DB, timeout=120)


def clip(text: str, limit: int = MAX_PASSAGE_CHARS) -> str:
    """Trim to a length, but never through the middle of a word.

    Cutting on a character count left half-words at the end of every quoted
    passage, and the anachronism detector dutifully flagged them: *recr*,
    *viole*, *follo*, *radioactiv*. The detector was right and the quoting was
    wrong. Extractive answers are supposed to be incapable of anachronism, and
    that invariant is worth keeping exactly true, because it is the control the
    model is measured against.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit // 2 else limit]


def looks_like_index(text: str) -> bool:
    """Whether a passage is an index or a table of contents rather than prose.

    The volumes carry their own indexes and the segmenter treats them as entries,
    so retrieval happily returns things like *246 collector of eleCtricity, 2q4
    multiplier of eleCtricity, 255 ufe of*. Measured against real entries, two
    signals separate them cleanly: the share of tokens containing a digit, and
    commas per word. The thresholds are set well clear of ordinary prose, which
    tops out near 0.12 commas per word, because gazetteer entries legitimately
    carry coordinates and should not be thrown away with the indexes.
    """
    words = text.split()
    if len(words) < 8:
        return False
    numeric = sum(1 for w in words if any(c.isdigit() for c in w)) / len(words)
    commas = text.count(",") / len(words)
    return numeric > 0.25 or commas > 0.30


def content_words(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text))
            if w not in STOPWORDS and len(w) > 2]


# ---------------------------------------------------------------- vocabularies

def build_vocab(con: sqlite3.Connection) -> None:
    """One vocabulary per era, from that era's own corpus.

    This is what the anachronism detector consults, so it has to come from the
    era's text and nothing else. Stored gzipped, one token per line.
    """
    VOCAB_DIR.mkdir(parents=True, exist_ok=True)
    eras = [e for (e,) in con.execute(
        "SELECT DISTINCT era FROM chunks WHERE variant = 'norm'")]
    for era in sorted(eras, key=lambda e: ERAS.index(e) if e in ERAS else 99):
        counts: Counter[str] = Counter()
        cur = con.execute(
            "SELECT text FROM chunks WHERE era = ? AND variant = 'norm'", (era,))
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for (text,) in rows:
                counts.update(m.group(0).lower() for m in _WORD.finditer(text))
        path = VOCAB_DIR / f"{era}.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for w, n in counts.most_common():
                fh.write(f"{w}\t{n}\n")
        log.info("%-7s vocabulary: %d types, %d tokens", era, len(counts),
                 sum(counts.values()))


def load_vocab(era: str, min_count: int = 1) -> set[str]:
    path = VOCAB_DIR / f"{era}.txt.gz"
    if not path.exists():
        raise SystemExit(f"no vocabulary for {era}: run the vocab phase")
    out = set()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            w, _, c = line.rstrip("\n").partition("\t")
            if int(c or 1) >= min_count:
                out.add(w)
    return out


# ------------------------------------------------------------------- retrieval

def retrieve(con: sqlite3.Connection, era: str, terms: list[str],
             k: int = TOP_K) -> list[dict]:
    """Top passages for a question within one era. Nothing crosses the boundary."""
    if not terms:
        return []
    table = f"fts_{era}_norm"
    query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    try:
        rows = con.execute(f"""
            SELECT c.doc_id, c.headword, c.text, bm25({table}) AS score
            FROM {table} f JOIN chunks c ON c.chunk_id = f.rowid
            WHERE {table} MATCH ? ORDER BY score LIMIT ?""",
                           (query, k * 4)).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("%s: %s", era, e)
        return []
    out, seen = [], set()
    for doc_id, headword, text, score in rows:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        if looks_like_index(text):
            SKIPPED["index_like"] += 1
            continue
        out.append({"doc_id": doc_id, "headword": headword,
                    "text": clip(text), "score": round(score, 3)})
        if len(out) >= k:
            break
    return out


def coverage(question_terms: list[str], passages: list[dict]) -> float:
    """Share of the question's content words that appear in the passages at all."""
    if not question_terms:
        return 0.0
    pool = set()
    for p in passages:
        pool.update(content_words(p["text"]))
    return sum(1 for t in set(question_terms) if t in pool) / len(set(question_terms))


# ------------------------------------------------------------------ generators

def extractive_answer(question_terms: list[str], passages: list[dict],
                      max_sentences: int = 3) -> tuple[str, list[str]]:
    """Quote the sentences that carry the most of the question's terms.

    Cannot hallucinate and cannot be anachronistic, because every word of it was
    printed in that era. That is what makes it the control the model is measured
    against, not merely a fallback for when no model is available.
    """
    wanted = set(question_terms)
    scored = []
    for p in passages:
        for sent in _SENT.split(p["text"]):
            sent = sent.strip()
            if len(sent) < 25:
                continue
            hits = len(wanted & set(content_words(sent)))
            if hits:
                scored.append((hits, -len(sent), sent, p["doc_id"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    picked, cites, seen = [], [], set()
    for _hits, _neg, sent, doc_id in scored:
        key = sent[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        picked.append(f'"{sent}" [{doc_id}]')
        if doc_id not in cites:
            cites.append(doc_id)
        if len(picked) >= max_sentences:
            break
    return " ".join(picked), cites


class LocalGenerator:
    """A small instruction-tuned model, run on the CPU, kept on a short leash."""

    PROMPT = (
        "You are reading an encyclopaedia published in {era}. Answer the question "
        "using ONLY the passages below, in at most 60 words.\n"
        "Rules:\n"
        "- Use only what the passages say. Do not add anything you know.\n"
        "- Do not correct the passages, even if they are wrong.\n"
        "- If the passages do not answer the question, reply exactly: {refusal}\n\n"
        "Passages:\n{passages}\n\nQuestion: {question}\nAnswer:"
    )

    def __init__(self, model_id: str = DEFAULT_MODEL, max_new_tokens: int = 110,
                 dtype: str = "float32"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(os.cpu_count() or 4)
        self.torch = torch
        log.info("loading %s on cpu", model_id)
        self.tok = AutoTokenizer.from_pretrained(model_id)
        # float32 is the default deliberately. The obvious optimisation, halving
        # the weight traffic with bfloat16, made it several times *slower* on this
        # machine: PyTorch's CPU path for bf16 matmul falls back to something slow
        # without AMX or AVX512-BF16 hardware, so a 90 second answer became one
        # that had not finished after six minutes. Measure before believing.
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=getattr(torch, dtype))
        self.model.eval()
        self.max_new_tokens = max_new_tokens

    def answer(self, era: str, question: str, passages: list[dict]) -> str:
        body = "\n".join(f"[{p['doc_id']}] {p['text']}" for p in passages)
        prompt = self.PROMPT.format(era=era, refusal=REFUSAL, passages=body,
                                    question=question)
        msgs = [{"role": "user", "content": prompt}]
        text = self.tok.apply_chat_template(msgs, tokenize=False,
                                            add_generation_prompt=True)
        ids = self.tok(text, return_tensors="pt")
        with self.torch.no_grad():
            out = self.model.generate(
                **ids, max_new_tokens=self.max_new_tokens, do_sample=False,
                pad_token_id=self.tok.eos_token_id)
        return self.tok.decode(out[0][ids["input_ids"].shape[1]:],
                               skip_special_tokens=True).strip()


# ----------------------------------------------------------------------- runs

def anachronisms(answer: str, vocab: set[str], question_terms: set[str]) -> list[str]:
    """Content words in the answer that the era's own corpus never uses.

    Words from the question itself are exempt: they were put in the model's mouth
    by the asking, so counting them would measure the question, not the answer.
    """
    out = []
    for w in dict.fromkeys(content_words(answer)):
        if w not in vocab and w not in question_terms:
            out.append(w)
    return out


def load_questions() -> list[dict]:
    doc = yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8")) or {}
    return doc.get("questions", [])


def run(con: sqlite3.Connection, generator: LocalGenerator | None,
        eras: list[str], top_k: int = TOP_K) -> list[dict]:
    questions = load_questions()
    vocabs = {era: load_vocab(era) for era in eras}
    rows = []
    for q in questions:
        q_terms = content_words(q["ask"])
        head_terms = [w for h in (q.get("headwords") or []) for w in content_words(h)]
        terms = list(dict.fromkeys(q_terms + head_terms))
        for era in eras:
            passages = retrieve(con, era, terms, top_k)
            cov = coverage(q_terms, passages)
            record = {"question": q["id"], "kind": q.get("kind", ""), "era": era,
                      "n_passages": len(passages), "coverage": round(cov, 3),
                      "top_headwords": [p["headword"] for p in passages[:3]]}
            if not passages or cov < MIN_COVERAGE:
                record.update({"mode": "abstain_retrieval", "answer": REFUSAL,
                               "citations": [], "anachronisms": []})
                rows.append(record)
                continue

            if generator is None:
                answer, cites = extractive_answer(q_terms, passages)
                mode = "extractive"
            else:
                answer = generator.answer(era, q["ask"], passages)
                cites = [p["doc_id"] for p in passages
                         if p["doc_id"] in answer] or [passages[0]["doc_id"]]
                mode = "local"
            if not answer or REFUSAL.lower() in answer.lower():
                record.update({"mode": "abstain_" + mode, "answer": REFUSAL,
                               "citations": [], "anachronisms": []})
                rows.append(record)
                continue
            flags = anachronisms(answer, vocabs[era], set(terms))
            record.update({"mode": mode, "answer": answer, "citations": cites,
                           "anachronisms": flags})
            rows.append(record)
            log.info("%-16s %-7s %-11s cov %.2f  flags %d", q["id"], era, mode,
                     cov, len(flags))
    return rows


def write_outputs(rows: list[dict], suffix: str) -> None:
    path = PROC / f"answers{suffix}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = TABLES / f"answers{suffix}.csv"
    with summary.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["question", "kind", "era", "mode", "coverage", "n_anachronisms",
                    "anachronisms", "citations", "answer"])
        for r in rows:
            w.writerow([r["question"], r["kind"], r["era"], r["mode"], r["coverage"],
                        len(r["anachronisms"]), " ".join(r["anachronisms"][:6]),
                        " ".join(r["citations"][:3]), r["answer"][:400]])

    if SKIPPED:
        log.info("passages skipped as index or table material: %d",
                 SKIPPED["index_like"])
    answered = [r for r in rows if not r["mode"].startswith("abstain")]
    flagged = [r for r in answered if r["anachronisms"]]
    log.info("%d question-era pairs: %d answered, %d abstained", len(rows),
             len(answered), len(rows) - len(answered))
    if answered:
        log.info("anachronism rate: %d of %d answers (%.0f%%), %.2f flagged words each",
                 len(flagged), len(answered), 100 * len(flagged) / len(answered),
                 sum(len(r["anachronisms"]) for r in answered) / len(answered))

    # Abstention scored against the question set: the `absent` questions are the
    # ones where declining is the right answer before 1911.
    rate = TABLES / f"abstention{suffix}.csv"
    with rate.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "era_group", "pairs", "abstained", "share"])
        for kind in sorted({r["kind"] for r in rows}):
            for group, test in (("pre_1911", lambda e: e not in ("1911", "modern")),
                                ("1911_and_modern", lambda e: e in ("1911", "modern"))):
                sel = [r for r in rows if r["kind"] == kind and test(r["era"])]
                if not sel:
                    continue
                ab = sum(1 for r in sel if r["mode"].startswith("abstain"))
                w.writerow([kind, group, len(sel), ab, round(ab / len(sel), 3)])
    log.info("answers -> %s, %s, %s", path, summary, rate)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phases", default="vocab,answer")
    ap.add_argument("--generator", choices=["extractive", "local"], default="extractive")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dtype", default="float32", choices=["bfloat16", "float32"])
    ap.add_argument("--max-new-tokens", type=int, default=90)
    ap.add_argument("--top-k", type=int, default=4,
                    help="passages per question; the prompt dominates CPU time")
    ap.add_argument("--eras", default="", help="comma separated subset, default all")
    args = ap.parse_args()
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}

    con = connect()
    if "vocab" in phases:
        build_vocab(con)
    if "answer" not in phases:
        return 0

    have = {e for (e,) in con.execute(
        "SELECT DISTINCT era FROM chunks WHERE variant = 'norm'")}
    eras = [e for e in ERAS if e in have]
    if args.eras:
        wanted = {e.strip() for e in args.eras.split(",")}
        eras = [e for e in eras if e in wanted]

    generator = (LocalGenerator(args.model, args.max_new_tokens, args.dtype)
                 if args.generator == "local" else None)
    rows = run(con, generator, eras, args.top_k)
    write_outputs(rows, "" if generator is None else "_local")
    return 0


if __name__ == "__main__":
    sys.exit(main())

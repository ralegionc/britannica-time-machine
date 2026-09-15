"""Link the entry for one subject across twelve layers.

This is the spine of the whole project. Everything downstream assumes it can ask
"what did each era say about X" and get back one entry per era; nothing can do
that until the entries are joined, and joining them is harder than matching
strings because the strings move. A subject changes spelling (CHYMISTRY to
CHEMISTRY), changes name outright (CONSUMPTION to TUBERCULOSIS), splits, merges,
and in the modern layer is titled by a convention nobody in 1771 would recognise.

Five routes do the joining, and each link records which one made it so that the
weak ones can be audited or dropped:

`exact`      the normalised headword is the same in both eras. Carries most of
             the weight, because the headword survives OCR: the long s is a
             lowercase letter and headwords are set in capitals.
`variant`    the encyclopaedia declaring its own synonyms. Entries open
             `ABAPTISTON, or Anabaptiston, an obsolete term...` and
             `ABBREVIATION, or Abbreviature, implies...`, which is the compilers
             saying these are one subject under two names. There are thousands,
             they cost nothing to mine, and they are better evidence than any
             string heuristic because they are the period's own judgement.
             Pure pointers (`ABBAT. See Abbot.`) were expected to be the richer
             seam and are not: 13 of them against 4,767 variant declarations in
             the same four editions, and most of the 13 point at a treatise
             rather than a synonym (`See the Index subjoined to Medicine`), so
             that route is not used.
`alias`      declared in eval/aliases.yaml, for renamings no string distance
             reaches: NIGRITIA to WEST AFRICA, FIXED AIR to CARBON DIOXIDE.
`wikipedia`  the `wikipedia=` parameter in the EB1911 header, which is the
             Wikisource editors' own mapping from a 1911 headword to a modern
             title. It is how *Gravitation* finds *Gravity*.
`fuzzy`      edit distance one, plus a test that decides whether the difference
             is damage or meaning: exactly one of the two spellings may be a word
             the 1911 lexicon knows. Auditing this route without that test found
             it about half wrong, and the reason is that one edit is not a large
             distance in English. BARON and BACON are one apart. So are ANEMIA
             and ANOMIA, a disease and a brachiopod; PARRY and PARTY; BOLAS and
             BOLUS; RIBALD and RIBAND. What every one of those pairs has in
             common is that both spellings are real words. When only one is, the
             other is almost always the scanner or the century talking:
             DENDERMOND for DENDERMONDE, VOLHINIA for VOLHYNIA, ARCKILUTE for
             ARCHILUTE, POT ASH for POTASH.

A sixth route, filing a 1911 biography under its surname so that `Abbot, George`
could meet an earlier `ABBOT`, is implemented and **off by default**. Audited, it
merged people who share a surname with each other and with unrelated subjects:
`JONES, Owen` with every other Jones, `HAKE, Thomas Gordon` with the fish. Turn
it on with `--prefix` if you want it; the damage is in the linking_stats table
either way.

The output is one row per (subject, era, entry), which is what `s07` indexes and
`s09` diffs.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict

import yaml

from common import (EB1911_DB, ERAS, EVAL, INTERIM, PROC, TABLES, get_logger)

log = get_logger("s06")

MODERN_DB = INTERIM / "modern.sqlite"
SUBJECTS = PROC / "subjects.jsonl"

# "ABAPTISTON, or Anabaptiston, an obsolete term for..." -- the entry's own
# declaration that two names are one subject. It has to sit immediately after the
# headword: an "or" later in the prose is an ordinary disjunction, and a "See X"
# later is a topical pointer to a different subject, which would merge anatomy
# into every anatomical term if it were followed.
_VARIANT = re.compile(r"^or\s+([A-Z][A-Za-zÆæ'\-]{2,30})\s*[,.]")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACE = re.compile(r"\s+")

FUZZY_MIN_LEN = 6
# How many distinct headwords a component may reach through the weak routes.
# Generous for a real subject with several names, tight enough to stop a chain.
WEAK_ROUTE_CAP = 4


def norm(s: str) -> str:
    """Fold a headword to a comparable key.

    Case, accents, ligatures and punctuation all vary across 250 years of
    typesetting and two OCR pipelines; none of them distinguish subjects.
    """
    if not s:
        return ""
    s = s.replace("Æ", "AE").replace("æ", "ae")
    s = s.replace("Œ", "OE").replace("œ", "oe")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s.upper())
    return _SPACE.sub(" ", s).strip()


class DSU:
    """Union-find over nodes, tracking the distinct headwords in each component.

    The headword set is not bookkeeping for its own sake. Union-find is
    transitive and the weaker routes are not: each of `BAR, or Barr`,
    `BARR, or Barre`, `BARRE, or Barri` is a defensible link on its own, and
    following all of them merges five different places into one subject. Weak
    routes therefore ask `would_exceed` first and decline to grow a component
    past a cap, which stops a chain without having to decide which of its links
    was the wrong one.
    """

    def __init__(self):
        self.parent: dict[str, str] = {}
        self.keys: dict[str, set[str]] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.keys[x] = {x.split("\t", 1)[-1]}

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def would_exceed(self, a: str, b: str, cap: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        return len(self.keys[ra] | self.keys[rb]) > cap

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if len(self.keys[ra]) < len(self.keys[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.keys[ra] |= self.keys.pop(rb)
        return True


def within_one(a: str, b: str) -> bool:
    """True when `a` and `b` are at Levenshtein distance one or zero."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def load_nls() -> tuple[dict[str, dict[str, list[str]]], list[tuple[str, str, str]]]:
    """Per era, normalised headword to entry ids; plus the mined variant names."""
    per_era: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    variants: list[tuple[str, str, str]] = []
    files = sorted(PROC.glob("entries_*.jsonl"))
    if not files:
        raise SystemExit("no segmented entries: run s05 first")
    for path in files:
        era = path.stem.split("_", 1)[1]
        n = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                e = json.loads(line)
                key = norm(e["headword"])
                if not key:
                    continue
                eid = f"{era}:{e['volume_id']}:{e['entry_index']}"
                per_era[era][key].append(eid)
                n += 1
                body = e["text"][len(e["headword"]):].lstrip(" ,.;:—-")
                m = _VARIANT.match(body.split("\n", 1)[0].strip())
                if m:
                    target = norm(m.group(1))
                    if target and target != key:
                        variants.append((era, key, target))
        log.info("%s: %d entries, %d distinct headwords", era, n, len(per_era[era]))
    log.info("mined %d variant-name declarations from the entries themselves",
             len(variants))
    return per_era, variants


def load_1911() -> tuple[dict[str, list[str]], dict[str, str], dict[str, str]]:
    """Normalised headword to titles, the wikipedia alignment, and comma prefixes."""
    if not EB1911_DB.exists():
        raise SystemExit("no 1911 layer: run s03b first")
    con = sqlite3.connect(EB1911_DB)
    keys: dict[str, list[str]] = defaultdict(list)
    to_modern: dict[str, str] = {}
    prefix_of: dict[str, str] = {}
    for headword, wikipedia in con.execute(
            "SELECT headword, wikipedia FROM articles WHERE source IS NOT NULL"):
        key = norm(headword)
        if not key:
            continue
        keys[key].append(headword)
        if wikipedia:
            to_modern[key] = norm(wikipedia)
        # "Abbot, George" is a person; the early editions file him under ABBOT.
        # Recorded separately because it is the weakest of the joins.
        if "," in (headword or ""):
            head = norm(headword.split(",", 1)[0])
            if head and head != key:
                prefix_of[key] = head
    log.info("1911: %d distinct headwords, %d carry a wikipedia alignment",
             len(keys), len(to_modern))
    return keys, to_modern, prefix_of


def load_modern() -> dict[str, list[str]]:
    if not MODERN_DB.exists():
        log.warning("no modern layer yet: run s04b")
        return {}
    con = sqlite3.connect(MODERN_DB)
    keys: dict[str, list[str]] = defaultdict(list)
    for requested, title, missing in con.execute(
            "SELECT requested, title, missing FROM pages"):
        if missing:
            continue
        for label in {requested, title}:
            k = norm(label)
            if k:
                keys[k].append(title)
    log.info("modern: %d distinct titles", len(keys))
    return keys


def load_aliases() -> list[list[str]]:
    path = EVAL / "aliases.yaml"
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups = []
    for item in doc.get("aliases", []):
        members = {norm(item.get("canonical", ""))}
        members |= {norm(v) for v in item.get("variants", [])}
        members.discard("")
        if len(members) > 1:
            groups.append(sorted(members))
    log.info("aliases: %d declared groups", len(groups))
    return groups


def load_lexicon() -> set[str]:
    """The 1911 vocabulary, used here to tell OCR damage from a real word."""
    path = INTERIM / "lexicon.tsv"
    if not path.exists():
        log.warning("no lexicon: the fuzzy route needs one (run s03 --lexicon-only)")
        return set()
    with path.open(encoding="utf-8") as fh:
        return {line.split("	", 1)[0] for line in fh}


def fuzzy_links(keys: set[str], lexicon: set[str]) -> dict[str, str]:
    """Map a damaged or archaic spelling onto the one real word it is a copy of.

    Deliberately a mapping and not a set of pairs, because union-find is
    transitive and this route is not. When any two keys a single edit apart could
    link, CHAFERY, CHAFFER, CHAMFER and CHAFF all landed in one component with
    BEETLE and COLEOPTERA: a beetle, a forge, a haggler, a bevelled edge and
    cereal husks, joined end to end by links that each looked reasonable alone.
    Here a spelling the lexicon does not know may attach to exactly one spelling
    it does; two candidates means the evidence does not say which, so it attaches
    to neither, and two known words are never joined to each other at all.

    Candidates come from a deletion index, which turns an all-pairs comparison
    over 200,000 headwords into dictionary lookups.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        if len(k) < FUZZY_MIN_LEN:
            continue
        buckets[k].append(k)
        for i in range(len(k)):
            buckets[k[:i] + k[i + 1:]].append(k)

    known = {k for k in keys if k.lower() in lexicon}
    mapping: dict[str, str] = {}
    ambiguous = 0
    for k in keys:
        if len(k) < FUZZY_MIN_LEN or k in known:
            continue
        cands = set()
        for variant in [k] + [k[:i] + k[i + 1:] for i in range(len(k))]:
            for other in buckets.get(variant, ()):
                if other in known and other[0] == k[0] and within_one(k, other):
                    cands.add(other)
        if len(cands) == 1:
            mapping[k] = cands.pop()
        elif len(cands) > 1:
            ambiguous += 1
    log.info("fuzzy: %d damaged spellings mapped, %d rejected as ambiguous",
             len(mapping), ambiguous)
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fuzzy", action="store_true",
                    help="exact, variant, alias and wikipedia routes only")
    ap.add_argument("--prefix", action="store_true",
                    help="also file 1911 biographies under their surname (audited as unsafe)")
    args = ap.parse_args()

    per_era, variants = load_nls()
    k1911, to_modern, prefix_of = load_1911()
    kmodern = load_modern()
    alias_groups = load_aliases()

    # Every node is (era, normalised headword). Linking is over these, not over
    # entries, because one era can hold several entries for one subject.
    entries: dict[tuple[str, str], list[str]] = {}
    for era, keys in per_era.items():
        for key, ids in keys.items():
            entries[(era, key)] = ids
    for key, titles in k1911.items():
        entries[("1911", key)] = [f"1911:{t}" for t in titles]
    for key, titles in kmodern.items():
        entries[("modern", key)] = [f"modern:{t}" for t in sorted(set(titles))]

    dsu = DSU()
    routes: Counter[str] = Counter()
    node = lambda era, key: f"{era}\t{key}"  # noqa: E731

    for era, key in entries:
        dsu.add(node(era, key))

    present: dict[str, set[str]] = defaultdict(set)
    for era, key in entries:
        present[key].add(era)

    # 1. exact: the same headword in any two layers.
    for key, eras in present.items():
        if len(eras) < 2:
            continue
        first, *rest = sorted(eras)
        for era in rest:
            if dsu.union(node(first, key), node(era, key)):
                routes["exact"] += 1

    # 2. variant: the encyclopaedia declaring its own synonyms. The variant is
    # usually not an entry in the era that declares it, which is exactly the
    # point: another edition filed the subject under that name instead.
    for era, key, target in variants:
        # If the edition that declared the variant also gives it an entry of its
        # own, that edition is treating them as two subjects and this one is not
        # a synonym. BARR and BARRE are both towns with their own entries, and
        # believing every "or" between them chains five places into one.
        if (era, target) in entries:
            routes["variant_self_refuted"] += 1
            continue
        for other in present.get(target, ()):
            a, b = node(era, key), node(other, target)
            if dsu.would_exceed(a, b, WEAK_ROUTE_CAP):
                routes["variant_capped"] += 1
                continue
            if dsu.union(a, b):
                routes["variant"] += 1

    # 3. alias: declared renamings, joined wherever both members exist.
    for group in alias_groups:
        members = [(era, k) for k in group for era in present.get(k, ())]
        if len(members) < 2:
            continue
        head = members[0]
        for other in members[1:]:
            if dsu.union(node(*head), node(*other)):
                routes["alias"] += 1

    # 4. wikipedia: the curated 1911 to modern mapping.
    for key, modern_key in to_modern.items():
        if ("1911", key) in entries and ("modern", modern_key) in entries:
            if dsu.union(node("1911", key), node("modern", modern_key)):
                routes["wikipedia"] += 1

    # 5. prefix: off unless asked for. See the note at the top of this file.
    if args.prefix:
        for key, head in prefix_of.items():
            for era in present.get(head, ()):
                if era != "1911" and dsu.union(node("1911", key), node(era, head)):
                    routes["prefix"] += 1

    # 6. fuzzy: spelling drift, last and least.
    if not args.no_fuzzy:
        for damaged, real in fuzzy_links(set(present), load_lexicon()).items():
            eras_a, eras_b = present[damaged], present[real]
            if eras_a & eras_b:
                continue  # both spellings live in one era: probably two subjects
            a, b = node(sorted(eras_b)[0], real), node(sorted(eras_a)[0], damaged)
            if dsu.would_exceed(a, b, WEAK_ROUTE_CAP):
                routes["fuzzy_capped"] += 1
                continue
            if dsu.union(a, b):
                routes["fuzzy"] += 1

    # Collect the components.
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for era, key in entries:
        groups[dsu.find(node(era, key))].append((era, key))

    order = {e: i for i, e in enumerate(ERAS)}
    rows = []
    for i, (_root, members) in enumerate(sorted(groups.items())):
        eras = {e for e, _ in members}
        label = None
        for era in ("modern", "1911"):
            for e, k in members:
                if e == era:
                    label = k
                    break
            if label:
                break
        if not label:
            label = sorted(members, key=lambda m: -order.get(m[0], 0))[0][1]
        rows.append({
            "subject_id": f"s{i:06d}",
            "label": label,
            "n_eras": len(eras),
            "eras": sorted(eras, key=lambda e: order.get(e, 99)),
            "members": [{"era": e, "headword": k, "entries": entries[(e, k)]}
                        for e, k in sorted(members, key=lambda m: order.get(m[0], 99))],
        })

    with SUBJECTS.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    hist = Counter(r["n_eras"] for r in rows)
    log.info("%d subjects from %d (era, headword) nodes", len(rows), len(entries))
    log.info("links by route: %s", dict(routes))
    log.info("subjects by era span: %s", {k: hist[k] for k in sorted(hist)})

    with (TABLES / "linking_stats.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["measure", "key", "value"])
        for route, n in routes.most_common():
            w.writerow(["links_by_route", route, n])
        for span, n in sorted(hist.items()):
            w.writerow(["subjects_by_era_span", span, n])
        for era in ERAS:
            w.writerow(["subjects_present_in_era", era,
                        sum(1 for r in rows if era in r["eras"])])

    question_coverage(rows)
    log.info("subjects -> %s", SUBJECTS)
    return 0


def question_coverage(rows: list[dict]) -> None:
    """Which eras can actually answer each seed question. The point of the stage."""
    path = EVAL / "questions.yaml"
    if not path.exists():
        return
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_key: dict[str, dict] = {}
    for r in rows:
        for m in r["members"]:
            by_key.setdefault(m["headword"], r)

    out = []
    for q in doc.get("questions", []):
        heads = q.get("headwords") or [q["id"]]
        eras: set[str] = set()
        subjects = set()
        for h in heads:
            r = by_key.get(norm(h))
            if r:
                eras |= set(r["eras"])
                subjects.add(r["subject_id"])
        out.append({"id": q["id"], "kind": q.get("kind", ""),
                    "subjects": " ".join(sorted(subjects)),
                    "n_eras": len(eras),
                    "eras": " ".join(e for e in ERAS if e in eras)})
        log.info("  %-16s %-9s %2d eras  %s", q["id"], q.get("kind", ""),
                 len(eras), " ".join(e for e in ERAS if e in eras))
    with (TABLES / "question_coverage.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "kind", "subjects", "n_eras", "eras"])
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    sys.exit(main())

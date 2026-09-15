# Britannica Time Machine

**Question.** Ask one question of twelve encyclopaedias published between 1771
and today, each answering only from what it actually contains. What changed,
when did it change, and what was confidently wrong for how long?

**Approach.** One retrieval index per edition, sealed off from the others. A
question runs against all of them independently, so "what causes cholera" is
answered by the 1771 volumes out of miasma and bile, by 1860 out of contagion
and sanitation, by 1911 out of *Vibrio cholerae*, and by Wikipedia out of
molecular epidemiology. The output is not an answer. It is a row of answers with
the diff between them, and an era that has no entry on the subject is required
to say so rather than reach for the nearest passage.

The engineering problem is not the retrieval. It is that the eighteenth-century
layers arrive as raw OCR of long-s typography, which no tokeniser or embedding
model has ever seen, and as 195 volumes of undivided plain text with no article
boundaries in them at all.

## Status

Corpus construction, entry linking and the era-locked indexes are built and have
been run: `s01` to `s07`. All three layers are complete on disk:

| Layer | Contents |
|---|---|
| 1771 to 1860 | 193 NLS volumes, 198,323 entries across ten eras |
| 1911 | 36,757 articles reconstructed from the Wikisource dump, 229 MB |
| modern | 36,227 Wikipedia articles, 552 MB, 416 of them with no article |
| linked | 67,219 subjects, 13 of which have an entry in all twelve eras |
| indexed | 2,385,809 chunks in 22 era-locked FTS5 tables |

Answering, diffing and rendering are specified in
[notes/DESIGN.md](notes/DESIGN.md) and not yet built, as is the dense arm of the
retrieval ablation.

Every number below was produced by the code in `src/`.

## The data, verified before anything was built

| Source | What it gives | Verified |
|---|---|---|
| [NLS Data Foundry](https://data.nls.uk/data/digitised-collections/encyclopaedia-britannica/) | editions 1 to 8, 1771 to 1860, 195 volumes as OCR plain text | 353 MB zip, live, serves byte ranges |
| [Wikisource EB1911](https://en.wikisource.org/wiki/1911_Encyclop%C3%A6dia_Britannica) | the 11th edition, proofread, article by article | 38,771 article titles enumerated |
| Wikipedia | the modern layer | `prop=extracts`, aligned by title |

Full probe output, with the two findings that shaped the pipeline, is in
[notes/DATA_VERIFICATION.md](notes/DATA_VERIFICATION.md). The short version:

**The archive does not have to be downloaded.** It is one zip of 195 text files
and S3 serves ranges on it, so `s02` reads the zip's central directory over HTTP
and pulls individual volumes. One 4 MB volume takes about a second. The 353 MB
download is available as `--all` and there is never a reason to use it.

**The 1911 articles are empty.** Every EB1911 page on Wikisource is a header plus
a transclusion of the proofread `Page:` namespace, so `prop=extracts`, the
obvious plain-text API, returns an empty string with HTTP 200. Only
`action=parse` renders the body. That single fact turns the 1911 harvest from
one call per fifty articles into one call per article.

**The 1911 header is a gift.** It carries `wikipedia=`, a mapping from the 1911
headword to the modern Wikipedia title, curated by Wikisource editors. It is how
*Gravitation* finds *Gravity*, and it is half of the entry linking for free.
Measured on the first 250 articles harvested: **68% carry one**, and the rest
fall back to headword matching.

**`prop=extracts` lies about batching.** On Wikipedia it works, unlike on
Wikisource, but with `exlimit` above 1 it returns lead sections only, silently.
Batched at 20 titles per call the modern layer came back at 1.2 KB per article,
which is an abstract, not a corpus. `s04` therefore fetches full text one page
per call and keeps the batched path behind `--intro-only`. The batched path is
verified end to end; the full-text path was written after the quota above shut
this host out, so it is implemented and unrun.

## The era axis

All 195 volume titles parse (`src/common.py`), including the fourth edition's
`Volume 11 Part 1` shape and the two supplements, which name no edition of their
own and are where new subjects tended to land first.

| Edition | Era | Volumes |  | Edition | Era | Volumes |
|---|---|---|---|---|---|---|
| 1st | 1771 | 6 | | 5th | 1817 | 20 |
| 2nd | 1784 | 10 | | 6th | 1823 | 20 |
| 3rd | 1797 | 36 | | Supplement 4-6 | 1824 | 15 |
| Supplement 3 | 1801 | 4 | | 7th | 1842 | 22 |
| 4th | 1810 | 40 | | 8th | 1860 | 22 |

An era label is the edition's completion year, not the printing year, so the
1773 reprint of the first edition stays in the 1771 layer.

## Why the OCR is the project

Here is the 1771 preface, exactly as the scanner read it:

```
To diffufe the knowledge of Science, is the profefled defign of the fol¬
lowing work. What methods, it may be alked, have the compilers em¬
ployed to accomplilh this delign ?
```

Three problems, all of them fatal to retrieval if left alone:

1. **The long s is scanned as `f`**, and sometimes as `l`. Every content word in
   the layer is out of vocabulary: *fcience*, *diffufe*, *defign*.
2. **Line-end hyphenation** is marked `¬`, so *following* is two tokens.
3. **One line per printed line**, so paragraphs arrive pre-shredded.

`src/ocr.py` restores the long s with a lexicon rather than a rule: a token is
rewritten only when the original is absent from the lexicon and a substitution of
it is present, and a word-final `f` is never touched, because the long form was
never used at the end of a word. That is what keeps *of*, *if* and *himself*
intact while turning *poffeffion* into *possession*. The lexicon is built from
the 1911 layer, which is proofread, period-appropriate, and free of long s.

The headwords survive the scan clean, because the long s is a lowercase letter
and headwords are set in capitals. `ADHESION` comes through as `ADHESION`, never
as `ADHEFION`, and that asymmetry is what makes it possible to recover entry
boundaries from a volume of undivided text at all.

## A first result, from the corpus construction alone

193 volumes, 198,323 entries, ten eras. The out-of-vocabulary rate before and
after normalisation, per era, is the whole of `outputs/tables/segmentation_stats.csv`
in ten rows:

| Era | Volumes | Entries | OOV raw | OOV normalised | Tokens the long-s rule rewrites |
|---|---|---|---|---|---|
| 1771 | 6 | 26,763 | 14.3% | 4.9% | 9.7% |
| 1784 | 10 | 15,308 | 13.8% | 3.9% | 10.0% |
| 1797 | 36 | 33,606 | 14.3% | 4.8% | 9.1% |
| 1801 | 4 | 1,486 | 14.4% | 5.0% | 9.2% |
| 1810 | 40 | 20,924 | 14.4% | 4.8% | 9.9% |
| 1817 | 20 | 21,581 | 14.3% | 4.5% | 9.6% |
| 1823 | 20 | 22,943 | 2.2% | 2.1% | 0.0% |
| 1824 | 15 | 6,439 | 1.9% | 1.8% | 0.0% |
| 1842 | 21 | 26,238 | 2.2% | 2.1% | 0.0% |
| 1860 | 21 | 23,035 | 1.7% | 1.6% | 0.0% |

The break between 1817 and 1823 is not a change in scan quality or in the
pipeline. It is the long s leaving English printing. Through the fifth edition
the restoration rewrites about one token in ten and the volumes are full of
*fome*, *fuch* and *firft*; from the sixth edition onward it rewrites nothing,
because there is nothing to rewrite, and the raw text is already at the 2% floor
that ordinary OCR error sets.

That is a measurement of a typographic transition, made by a retrieval
pipeline, as a side effect of preparing the corpus. It also settles the design
question underneath the whole project: the early layers start seven times
noisier than the late ones, and normalisation closes most but not all of that
gap, so retrieval quality is expected to degrade backwards in time even after
the OCR work. Quantifying that curve is what `s07` is for.

## What the segmenter gets out of one volume

First edition, volume 1 (A to B), 4.0 MB of OCR, one plain text file with no
article structure in it:

| | |
|---|---|
| entries recovered | 3,889 |
| headwords starting with A or B | 97.2% |
| median entry length | 157 characters |
| running heads and folio numbers discarded | 1,749 lines |
| capitalised lines rejected as false headwords | 42 |
| entries dropped as too short to be entries | 251 |

The 97.2% is the check that matters: this is an A to B volume, so a segmenter
that were picking up noise would not produce a headword list that is almost
entirely alphabetical and almost entirely inside the volume's own letter range.
Everything rejected is counted rather than silently dropped, per volume, in
`outputs/tables/segmentation_stats.csv`.

## Measured, not assumed

Numbers from an actual smoke run on 2026-09-14, all reproducible from the code:

| | |
|---|---|
| API harvest throughput | 250 articles in 25 minutes, mostly `Retry-After` backoff |
| per-call latency, `action=parse` | 0.5s when the parser cache is warm, 24.7s when it is cold |
| quota behaviour | HTTP 429 on single unconcurrent calls, Wikipedia and Wikisource at once |
| **dump route, whole edition** | **38,771 stubs and 28,784 scan pages in 12 minutes, 1.15 GB** |
| articles reconstructed from scans | 36,757 (229 MB of prose); 2,009 are redirects or index pages |
| lexicon built from that layer | 190,791 types |
| NLS corpus | 193 volumes, 921 MB, fetched in under a minute |
| entries segmented | 198,323 across 10 eras |
| out-of-vocabulary rate, 1771 volume 1 | 13.2% raw, 4.3% after normalisation |
| 1911 articles carrying `wikipedia=` | 68% |

Two of those need saying plainly.

**The 1911 layer comes out of the dump, not the API, and it is seekable.**
The multistream dump is a concatenation of independent bzip2 streams of 100
pages each, and Wikimedia publishes an index saying which stream every page is
in. Of 47,521 streams, 10,389 hold something this project wants, so `s03b`
fetches those byte ranges and leaves 2.5 GB on the server. Merging ranges
separated by less than 256 KB turns 4,290 requests into 1,477, at a cost of
0.30 GB of wasted download, and both ends of that trade are a flag.

**Neither layer can be harvested through the API at scale.** Rendering a
transcluded DjVu page is slow when it is not cached, but the binding constraint
is the anonymous Wikimedia quota: after 250 articles every call from this host
returned HTTP 429, on Wikipedia and Wikisource at the same time, including
single unconcurrent ones. The quota is per client across projects. The answer is
the published dumps, which carry the EB1911 articles, the `Page:` namespace they
transclude, and the modern layer, with no quota and no rendering. It is
specified in [notes/DESIGN.md](notes/DESIGN.md); the API stages stay for samples
and top-ups. An authenticated account raises the ceiling but does not remove it.

**The lexicon is what makes the eighteenth century readable.** Long-s
restoration can only rewrite a token into a word the lexicon knows, so the
measurement moves with the lexicon behind it. On a 250 article sample (7,396
types) the 1771 volume came out at 30.3% out of vocabulary, falling to 24.0%.
On the full 1911 layer from the dump (190,791 types) the same volume measures
13.2%, falling to **4.3%**: the restoration now removes two thirds of the
unknown tokens, and what remains is real OCR damage rather than missing
vocabulary. The residue is dominated by the confusions no lexicon rule can
reach: *produdt*, *fradtion*, *fubjedt*, *ihould*, *firfl*.

Two measurement bugs turned up on the way to that number, both of which had been
flattering or damning the result without changing the pipeline. The lexicon
builder tokenised with a different regex than the measurement, which put every
single-letter word permanently out of vocabulary and added 6.7 points to every
rate. And the sample was the first 200 entries of each volume, which is the
title-page and preface end of the book where scanner noise and handwritten
flyleaf marginalia live: striding across the volume instead, and weighting by
token rather than averaging per-entry rates, moved the same measurement from
24.0% to 13.2% before a line of the OCR code changed.

## The curve this project was built to measure

One FTS5 index per era, 2,385,809 chunks, and the same 300 queries run against
every one of them. A query is the opening of a modern Wikipedia article **with
the subject's own name removed**, so retrieval has to work from the description
rather than from the headword; the gold answer is whichever entry `s06` linked to
that subject in that era.

| Era | Queries | recall@10 normalised | recall@10 raw | gain | MRR norm | MRR raw |
|---|---|---|---|---|---|---|
| modern | 300 | **1.000** | | | 1.000 | |
| 1911 | 298 | 0.480 | | | 0.308 | |
| 1860 | 196 | 0.321 | 0.321 | +0.000 | 0.180 | 0.176 |
| 1842 | 233 | 0.223 | 0.223 | +0.000 | 0.115 | 0.112 |
| 1824 | 22 | 0.318 | 0.318 | +0.000 | 0.177 | 0.172 |
| 1823 | 288 | 0.233 | 0.233 | +0.000 | 0.105 | 0.105 |
| 1817 | 283 | 0.198 | 0.134 | **+0.064** | 0.099 | 0.055 |
| 1810 | 276 | 0.207 | 0.134 | **+0.073** | 0.096 | 0.054 |
| 1801 | 10 | 0.400 | 0.300 | +0.100 | 0.258 | 0.250 |
| 1797 | 288 | 0.163 | 0.104 | **+0.059** | 0.079 | 0.036 |
| 1784 | 259 | 0.189 | 0.135 | **+0.054** | 0.084 | 0.045 |
| 1771 | 225 | 0.182 | 0.142 | **+0.040** | 0.084 | 0.062 |

Three things to read off it.

**The control is exact.** The modern era scores 1.000 at rank one. The query is
drawn from the modern article, so it had better, and the fact that it does is
what licenses reading the rest of the column: the low early numbers are the task
being hard, not the harness being broken.

**Retrieval degrades smoothly backwards in time**, from 1.000 today to 0.480 in
1911, 0.321 in 1860, and about 0.18 in 1771. Asking an eighteenth-century
encyclopaedia a modern question in modern words is roughly five times harder than
asking 1911 and twenty times harder than asking Wikipedia, on the same index,
the same metric and the same 300 questions.

**The normalisation gain stops dead in 1823.** It is worth +0.04 to +0.07
recall@10 through 1817, and it nearly doubles MRR at 1797 (0.036 to 0.079) and
1810 (0.054 to 0.096). Then at 1823, 1824, 1842 and 1860 it is **+0.000, four
times in a row.** That is the long s leaving English printing, measured a second
time by a completely different instrument: `s05` found the same boundary in the
out-of-vocabulary rate, which falls from 14% to 2% between the fifth edition and
the sixth. Two independent measurements, one typographic transition, the same
five years.

What the numbers do not support is any claim about dense retrieval, which is not
built. And the supplements (1801, 1824) have ten and twenty-two queries; they are
in the table for completeness and should not be read as trends.

## Linking one subject across twelve layers

67,219 subjects out of 210,007 (era, headword) nodes. Five routes, each recorded
on the link it made, so a weak one can be audited or dropped:

| Route | Links | What it is |
|---|---|---|
| `exact` | 118,528 | the same normalised headword in two layers |
| `wikipedia` | 21,323 | the `wikipedia=` parameter in the 1911 header |
| `fuzzy` | 1,920 | one edit, gated on the lexicon |
| `variant` | 996 | the encyclopaedia declaring its own synonym |
| `alias` | 21 | declared in `eval/aliases.yaml` |

and three counters for what was refused: 1,457 variant declarations vetoed by
the edition that made them, 413 fuzzy candidates rejected as ambiguous, 27
merges declined for growing a component too far.

Subjects reaching back through the eras thin out exactly as you would expect:
28,263 appear in two, 1,264 in ten, 156 in eleven, and 13 in all twelve.

### Four things the audit changed

**The corpus declares its own synonyms.** Entries open `ABAPTISTON, or
Anabaptiston, an obsolete term...`, and 11,095 of those declarations are sitting
in the text already, written by people who knew the subject. The pure cross
reference this project expected to mine instead, `ABBAT. See Abbot.`, barely
exists: 13 against 4,767 in the same four editions, and most of the 13 point at
a treatise rather than a synonym.

**One edit is not a small distance in English.** Ungated, the fuzzy route was
about half wrong. ANEMIA and ANOMIA are one apart and are a disease and a
brachiopod; so are BARON and BACON, PARRY and PARTY, BOLAS and BOLUS. The fix is
not a tighter distance but a different question, asked of the 1911 lexicon: are
*both* spellings words? If so it is two subjects. If only one is, the other is
the scanner or the century talking, and PLNDUS to PINDUS, JUNIPERQS to
JUNIPERUS, MOOR HEN to MOORHEN all come through.

**Union-find is transitive and this evidence is not.** CHAFERY, CHAFFER, CHAMFER
and CHAFF landed in one component with BEETLE and COLEOPTERA, joined end to end
by links that each looked reasonable alone. Two rules bound it: the declaring
edition gets a veto (`BARR, or Barre` is dropped when that edition also gives
BARRE its own entry), and weak routes may not grow a component past a cap.

**A route worth deleting.** Filing 1911 biographies under their surname, so that
`Abbot, George` could meet an earlier `ABBOT`, merged every Jones with every
other Jones and `Hake, Thomas Gordon` with the fish. It is off by default.

Linking also exposed a bug upstream. Forty-six unrelated biographies had become
one subject because 45 articles carry `|wikipedia = {{import enwiki|...}}` and
the header reader took it literally. `wikitext.header_param` now refuses values
containing template or comment syntax, and the 1911 layer was rebuilt.

### What the seed questions can actually be asked of

`outputs/tables/question_coverage.csv`, in full:

| Question | Kind | Eras with an entry |
|---|---|---|
| continents, species, light, atom, electricity, africa_interior | replaced, refined, expanded | all 12 |
| heat | replaced | 11 (no 1824 supplement entry) |
| dna | absent | 10, under GENERATION and HEREDITY |
| cholera | replaced | 4: 1842, 1860, 1911, modern |
| radioactivity | absent | 2: 1911, modern |

The last two are the interesting rows. Radioactivity behaves exactly as the
question set predicted, appearing in 1911 and never before. Cholera does not:
the early editions have no cholera entry at all, so the 1771 answer to "what
causes cholera" is not a wrong theory under a different headword, it is silence.
That is a finding about what an encyclopaedia of 1771 was for, and it is also
precisely the abstention case `s08` has to get right.

## When seeking into a dump pays, and when it does not

Both bulk stages had the same choice to make and they did not make it the same
way, which turned out to be the more interesting result.

| | 1911 layer (Wikisource) | Modern layer (Wikipedia) |
|---|---|---|
| dump size | 3.64 GB | 26.80 GB |
| streams in the dump | 47,521 | 258,634 |
| streams holding wanted pages | 10,389 (22%) | 15,244 (5.9%) |
| selective fetch | 1.15 GB in 1,477 ranges | 3.84 GB in 6,257 ranges |
| bulk API alternative | none: one call per article, quota-bound | 727 calls, 1.4 GB |
| **chosen** | **dump** | **API** |

The 1911 layer has no bulk API, because an article's body is a transclusion and
only `action=parse` renders it, one page at a time, against a quota that shut
this host out after 250 articles. The dump is the only way to get the edition.

The modern layer does have one. `prop=revisions` returns full wikitext for 50
titles per call, so the whole layer is about 700 requests, and the wikitext is no
obstacle because `wikitext.py` already converts it for the 1911 layer.

I priced the dump route for the modern layer anyway, and got the prediction
wrong in the direction worth recording. I expected these titles to be scattered
across nearly every stream, since they were written by unrelated people over
twenty years. They are not: 5.9% of streams, 14% of the dump. Page id order is
roughly creation order, and the articles a 1911 encyclopaedia has entries for
are mostly old ones, written early, sitting near each other in the file. The
dump route stays in the stage as a genuine fallback rather than a straw man.

## Checking the dump route against the API

The two routes to the 1911 layer are independent: one asks MediaWiki to render a
transclusion, the other reconstructs it offline from the same scan pages. Where
both have fetched the same article, `s03b --phases validate` compares them.

| API article length | n | token overlap | length ratio |
|---|---|---|---|
| under 1,000 | 64 | 0.64 | 0.61 |
| 1,000 to 5,000 | 128 | 0.83 | 0.85 |
| 5,000 to 20,000 | 20 | 0.92 | 0.95 |
| over 20,000 | 8 | 0.89 | 0.98 |

Agreement rises with length and the dump text is consistently the shorter of the
two, which is the signature of a fixed overhead per article rather than content
lost offline. That overhead is the API rendering's own furniture: breadcrumbs,
the previous and next headwords, "Wikidata item", the project disclaimer. On a
400 character entry it is most of the text; on a 20,000 character one it
disappears. The offline reconstruction is the cleaner corpus of the two, which
is the opposite of what the comparison was set up expecting.

Two real defects surfaced here before that conclusion could be drawn, and both
are worth knowing about if you write something similar. Deleting wikitables
outright cost the *Abbreviation* article 21 KB of its 23 KB, because the article
is a table. And dropping unrecognised templates whose parameters were too short
to look like prose was quietly deleting cross-references, leaving holes in the
middle of sentences: *are common in ancient writings and inscriptions (see and
), and very many are in use at the present time*.

## Pipeline

```bash
pip install -r requirements.txt
python run_all.py --quick      # API sample, no large download
python run_all.py              # the whole 1911 edition from the dump, about 15 minutes
```

| Stage | What it does |
|---|---|
| `s01_verify_sources.py` | probes every source, writes `outputs/tables/source_verification.csv` |
| `s02_fetch_nls.py` | reads the zip central directory over HTTP, pulls volumes by byte range |
| `s03b_fetch_eb1911_dump.py` | builds the whole 1911 layer offline from the Wikisource dump |
| `s03_fetch_eb1911.py` | the same layer via `action=parse`, for samples and top-ups; builds the lexicon |
| `s04b_fetch_modern_bulk.py` | modern layer in bulk, 50 titles per call, or from the Wikipedia dump |
| `s04_fetch_modern.py` | the same layer one article at a time; kept as the comparison case |
| `s05_segment_entries.py` | cuts volumes into entries, normalises the OCR, reports what it rejected |
| `s06_link_entries.py` | joins the entry for one subject across all twelve layers |
| `s07_build_indexes.py` | chunks the corpus, builds one FTS5 index per era, runs the ablation |
| `s08` to `s10` | era-locked answering, diffing, rendering. See [notes/DESIGN.md](notes/DESIGN.md) |

Every stage resumes. `s03` and `s04` are the slow ones and both take `--limit`.

## What makes this more than a demo

Three things are measurable here that a chat-with-your-PDF project cannot
measure:

- **Retrieval degradation over 250 years.** The same questions, the same
  metrics, twelve eras. Raw OCR against normalised text, crossed with BM25,
  dense and hybrid retrieval. Nobody has published that curve.
- **Abstention as a first-class result.** Ask 1771 what carries hereditary
  information. The correct behaviour is to refuse, and the question set is built
  so that refusal can be scored.
- **Anachronism detection.** A modern model asked to answer as 1771 will happily
  mention bacteria. Every content word in a generated answer is checked against
  the vocabulary of that era's own corpus, so a fluent anachronism is caught
  automatically rather than admired.

## Layout

```
src/            pipeline stages, numbered in run order
  common.py     paths, HTTP, rate limiting, the edition table
  ocr.py        dehyphenation, long-s restoration, reflowing
  wikitext.py   transclusion, section slicing, wikitext to prose
eval/           question set and the table of subjects that changed name
notes/          data verification and the design of the unbuilt stages
outputs/tables/ verification and segmentation statistics
data/           not in git; every stage re-fetches from the original hosts
```

## Licence and sources

Code is MIT. The corpora are not redistributed here. NLS text up to 1853 is
public domain, 1854 to 1860 carries a No Known Copyright statement; Wikisource
and Wikipedia are CC BY-SA. Please keep a contact address in `BTM_USER_AGENT`
if you run the harvest stages at volume.

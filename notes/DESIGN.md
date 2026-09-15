# Design

## The claim

An encyclopaedia is a snapshot of what an educated reader was expected to
believe. Ten of them, published between 1771 and now, make the same question
answerable ten times over, and the differences between the answers are the
history of an idea. The retrieval system exists to make that comparison
mechanical rather than anecdotal.

The unit of output is not an answer. It is a row of answers, one per era, each
built only from what that era's volumes contain, plus the diff between them.

## Era-locked retrieval

One index per era. A query runs against each index independently and an answer
is generated per era from that era's passages alone. Nothing crosses the line:
no shared vocabulary statistics, no fallback to a later era when an earlier one
retrieves badly, no "best available" passage. An era that cannot answer must
abstain, because abstention is the finding for questions like *what carries
hereditary information* asked of 1771.

| Layer | Source | Entries |
|---|---|---|
| 1771, 1784, 1797, 1801, 1810, 1817, 1823, 1824, 1842, 1860 | NLS Data Foundry OCR | segmented by `s05` |
| 1911 | Wikisource EB1911 | 37,420 pages |
| modern | Wikipedia, current | aligned subset |

## Harvesting the full 1911 layer from the dump (s03b, built)

`s03` fetches articles one at a time through `action=parse`, because the body of
an EB1911 page is a transclusion and no batched API renders it. That is the
right tool for a sample and the wrong one for an edition: the observed rate was
10 articles per minute, and most of that was backoff against the anonymous
Wikimedia quota, which is shared across projects and which shut this host out of
both Wikipedia and Wikisource after 250 articles.

The offline route uses `enwikisource-latest-pages-articles.xml.bz2`, which
contains both halves of the transclusion, because `Page:` is a content namespace
on Wikisource:

1. Stream the dump, keeping pages whose title starts with the EB1911 prefix and
   every `Page:EB1911 - Volume NN.djvu/PPP`.
2. For each article, parse its `<pages ... />` tag for the volume, the pages and
   the section labels. Both forms occur and `include=` is the commoner one, with
   disjoint page lists (`include=687-688,690`), `exclude`, `step`, and
   `onlysection` for an article that shares every one of its pages with a
   neighbour.
3. Concatenate the wikitext of those `Page:` records, honouring
   `<section begin="X"/>` and `<section end="X"/>` markers, which is how two
   articles that share a printed page are separated.
4. Strip proofreading templates and headers, then emit the same rows `s03`
   writes, so everything downstream is unchanged.

No quota, no server-side rendering, and it reproduces exactly.

Implemented in `src/s03b_fetch_eb1911_dump.py`, with one addition the sketch
above missed: the dump does not have to be downloaded whole. The multistream
variant is seekable, the published index says which of its 47,521 streams holds
each page, and only 10,389 of them hold EB1911 material. The stage plans byte
ranges from the index and fetches 1.15 GB instead of 3.64 GB. Transclusion
semantics live in `src/wikitext.py`.

The modern layer should come the same way, from the Wikipedia dump or the
Enterprise HTML snapshots, rather than 38,771 extract calls. The API paths stay
for incremental top-ups, since a dump is a monthly snapshot.

## Chunking

Entries are the natural chunk and most are short. The systematic treatises are
not: *Chemistry*, *Anatomy* and *Optics* run to dozens of printed pages in the
early editions, and a whole treatise is useless as a retrieval unit. Entries over
roughly 6,000 characters are split on paragraph boundaries into overlapping
windows, keeping the headword as a prefix on every window so that a retrieved
fragment still says what it is an entry about.

## Entry linking (s06, built)

The spine of the timeline is a table of entry ids that refer to the same subject
across eras. Three routes, recorded per link so that the weak ones can be audited
separately:

1. **1911 to modern**: the `wikipedia=` parameter in the EB1911 header, curated
   by Wikisource editors. Handles renamings that string matching cannot
   (*Gravitation* to *Gravity*).
2. **Between NLS editions**: normalised headword equality, then edit distance one
   gated on the 1911 lexicon. The gate is the whole rule: one edit is not a
   large distance in English, so the test is not how far apart two spellings are
   but whether both are words. BARON and BACON are one apart and both real;
   VOLHINIA and VOLHYNIA are one apart and only one is.
3. **Declared aliases**: `eval/aliases.yaml`, for subjects that changed name
   outright (*NIGRITIA* to *WEST AFRICA*, *CONSUMPTION* to *TUBERCULOSIS*). This
   file is evidence, not configuration: it is the list of renamings the project
   found, and it belongs in the write-up.

Building it added a fourth route the sketch above missed, and it turned out to
be the best one available: **the encyclopaedia declares its own synonyms**.
Entries open `ABAPTISTON, or Anabaptiston, an obsolete term...`, and 11,095 of
those declarations sit in the corpus already, written by people who knew the
subject. What the sketch expected instead, the pure cross reference
`ABBAT. See Abbot.`, barely exists: 13 of them against 4,767 variant
declarations across the same four editions, and most of the 13 point at a
treatise rather than a synonym.

Two rules keep the weak routes from doing damage, both added after auditing:

- **The declaring edition gets a veto.** If an edition says `BARR, or Barre`
  but also gives BARRE its own entry, that edition is treating them as two
  subjects and the link is dropped. 1,457 declarations are refuted this way.
- **Components cannot grow without limit through weak links.** Union-find is
  transitive and this evidence is not: each of `BAR, or Barr`, `BARR, or Barre`,
  `BARRE, or Barri` is defensible alone, and following all of them merges five
  places into one subject.

## Retrieval (s07, BM25 arm built)

BM25 over the normalised text is the baseline and, for the early eras, probably
the ceiling: embedding models have seen very little eighteenth-century orthography
and nothing at all of this scanner's failure modes. Dense retrieval runs
alongside it and the two are fused with reciprocal rank fusion.

The ablation is itself a result worth reporting, because it answers a question
nobody has a number for: **how much retrieval quality does 250 years of
typographic and lexical drift cost you?** The grid is raw OCR against normalised
text, crossed with BM25, dense, and hybrid, scored per era.

The BM25 half is built and run, and is in the README. The expected shape was a
curve degrading backwards in time and a normalisation gain growing with age. The
first half held: recall@10 falls from 1.000 today to 0.480 at 1911 and about
0.18 at 1771. The second half did not. The gain does not grow smoothly with age
at all; it is worth +0.04 to +0.07 through 1817 and then exactly zero at 1823,
1824, 1842 and 1860, because what it is really tracking is not age but the long
s, which left English printing between the fifth edition and the sixth.

Implemented against SQLite FTS5, so the index is a file and needs no dependency
beyond the standard library, and era locking is structural: one table per era,
with nowhere for a query to look outside its own.

The gold labels are not hand-made, which the design above assumed they would be.
They come from `s06`, which links on headwords and never reads body text, so
they are independent of what the retrieval is being scored on. Queries are the
openings of modern articles with the subject's own name stripped out, because
left in, the measurement is of headword matching rather than of reading.

The dense and hybrid arms are still unbuilt: encoding 2.4 million chunks on CPU
is hours of work for an arm whose result is largely foreseeable, since embedding
models have seen very little eighteenth-century orthography and none of this
scanner's failure modes.

## Answering (s08)

Retrieve top-k within the era, then generate an answer constrained to the
retrieved spans, with a citation to an entry id on every claim. Below a score
threshold the era returns "this edition has no entry that answers the question",
which is a first-class outcome, not a failure.

### Leakage is the central risk

A modern model knows that cholera is caused by a bacterium. Asked to answer as
1771, it will be tempted to say so, and a fluent anachronism is worse than no
answer because it looks like a finding. Three defences, in increasing order of
how much they are worth:

1. Instruction: answer only from the passages, do not explain, do not correct.
2. Extraction-first: the answer is assembled from quoted spans with minimal
   connective text, so unsupported sentences are visually obvious.
3. **An anachronism detector**: every content word in an answer is checked
   against the vocabulary of that era's own corpus. A 1771 answer containing
   *bacterium*, *germ* or *vibrio* is flagged automatically, because those tokens
   do not occur anywhere in the 1771 volumes. This is cheap, it is checkable by
   anyone, and the rate it reports is the number that makes the whole timeline
   trustworthy.

## Diff (s09)

Per question, across eras:

- the first era in which the modern explanatory term appears at all,
- entry length over time, which tracks how much a subject mattered,
- vocabulary turnover between consecutive eras (Jaccard over top TF-IDF terms),
- the abstention pattern, which should be a clean step function for the
  `absent` questions in `eval/questions.yaml`,
- terms that die: words present in early eras and absent later (*caloric*,
  *phlogiston*, *miasma*, *animalcule*).

## Render (s10)

One figure per question: a horizontal band per era in chronological order, each
carrying its answer, its citations, and a marker for abstention, with appearing
and dying terms picked out in colour down the whole timeline. Static PNG for the
README, plus an HTML version where each answer expands into the passages it was
built from and the scanned page they came from.

## Evaluation

| What | How | Why it matters |
|---|---|---|
| Retrieval | recall@k against hand-labelled gold entries per era | the OCR quality question, answered numerically |
| Abstention | false-answer rate on the `absent` questions | the honesty question |
| Anachronism | out-of-era vocabulary in generated answers | the leakage question |
| Faithfulness | manual audit of 20 sampled answers against cited spans | the part no metric covers |

## Known risks

- **Residual OCR damage.** Long-s restoration is lexicon-guided and conservative,
  so it leaves the harder confusions (*c* read as *e*, *dt* for *ct*) untouched.
  `s05` reports the residual out-of-vocabulary rate per volume; that number is
  the honest ceiling on everything downstream.
- **Segmentation inside treatises.** Long systematic articles carry internal
  headings in capitals that look exactly like headwords. They inflate the entry
  count and fragment treatises. Measurable against the printed index volumes,
  which the dataset includes and `s02` currently skips.
- **Uneven 1911 proofreading.** Wikisource coverage is not uniform across the 28
  volumes, so the 1911 layer is denser in some letter ranges than others.
- **Supplement era labels.** The supplements are dated to their completion year
  (1801, 1824), which is a simplification of a publication run that lasted years.

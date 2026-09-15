# Data verification

Run `python src/s01_verify_sources.py` to reproduce every check below. It writes
`outputs/tables/source_verification.csv`. The output here is from 2026-09-14.

```
PASS nls_dataset_page             HTTP 200
PASS nls_text_zip                 HTTP 200, 353 MB
PASS nls_zip_range_requests       HTTP 206, 1024 bytes returned
PASS eb1911_article_count         37,420 pages under the prefix
PASS eb1911_template_fields       315 chars of wikitext, volume and wikipedia params present
PASS eb1911_page_transclusion     article body is transcluded from the Page: namespace, so prop=text is required to read it
PASS wikipedia_extracts           54,433 chars for Cholera
```

## What each check protects

**`nls_text_zip` and `nls_zip_range_requests`.** The National Library of Scotland
publishes the first eight editions as a single 353 MB zip of 195 plain text
files, one per volume, plus an inventory. S3 serves byte ranges on it, which is
why `s02` can read the central directory and pull one 4 MB volume in about a
second instead of downloading the archive. If range support ever disappears the
check fails and `s02 --all` is the fallback.

**`eb1911_page_transclusion`.** This is the one that would have cost a day.
A 1911 article on Wikisource is a 300 character header plus a transclusion:

```
{{EB1911
|title     = [[../]]
|volume    = 6
|previous  = Choisy, François Timoléon
|next      = Cholet
|wikipedia = Cholera
}}
<pages index="EB1911 - Volume 06.djvu" from="278" to="283" fromsection="Cholera" tosection="Cholera" />
```

The body lives in the `Page:` namespace. `prop=extracts`, the obvious API for
plain text, returns an empty string for these pages, and returns it with HTTP
200, so it fails silently. Only `action=parse` renders the transclusion. The
cost is one API call per article rather than fifty articles per call, which is
what makes `s03` a multi-hour stage.

**`eb1911_template_fields`.** The `wikipedia=` parameter is a hand-curated
mapping from a 1911 headword to a modern Wikipedia title, contributed by
Wikisource editors. It handles the renamings that string matching cannot:
*Gravitation* to *Gravity*. It is not universal (*Slavery* has none), so `s04`
falls back to exact headword matching and records which route each alignment
came from.

## Throughput, measured

`s01` says the sources are live. It does not say they are fast, and one of them
is not. Timing three `action=parse` calls by hand:

```
Cholera          1.9s    59858 chars
Gravitation     24.7s    93250 chars
Abbey            0.5s   129405 chars
```

The spread is the parser cache: rendering a transcluded set of DjVu pages costs
real server time the first time anyone asks for it. Sustained throughput over
250 articles with 4 workers was 10 articles per minute, which puts the full
38,771 at tens of hours. `list=allpages` over the prefix is slow in its own
right (about 8 minutes for the full enumeration, and it degrades as the offset
deepens), so the title list is cached after the first run.

Most of it is not render cost. After the 250-article run, every Wikimedia API
call from this host started coming back:

```
HTTP 429  You are making too many requests to the API.
          Please follow the best practices at
          <https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits>
```

on single, unconcurrent requests, and on **both** en.wikipedia.org and
en.wikisource.org at once. The quota is per client across Wikimedia projects,
not per project, and the anonymous tier is small. The 10 articles per minute
measured during the harvest was mostly the client sitting in `Retry-After`
backoff against that quota, and the 24.7s parse call shows there is real
cold-render cost underneath it as well.

This is a hard constraint, not a tuning problem. Two ways through it, and the
second is the one the project should take:

1. Authenticate. An account with an OAuth token gets a substantially higher
   quota than anonymous traffic. It moves the ceiling; it does not remove it.
2. Stop using the API for bulk. Both layers exist as dumps: the Wikisource dump
   carries the EB1911 articles and the `Page:` namespace they transclude, and
   Wikipedia publishes the modern layer the same way. One download each, no
   quota, reproducible.

`s03` and `s04` keep the API path, with gentle defaults, for samples and for
topping up a corpus that a dump has already built.

Note also that enumeration and search disagree about corpus size: `list=search`
reports 37,420 hits for the prefix, `list=allpages` enumerates 38,771 article
titles after volume container pages are filtered out. The enumeration is the one
the pipeline uses.

## The dump route, measured

`s03b` reads the multistream index once and plans byte ranges from it. On the
September 2026 dump:

```
index lines                    4,751,918
EB1911 articles                   38,771     (the API enumerated 38,771 too)
Page:EB1911 scan records          28,784
streams in the dump               47,521
streams holding EB1911 material   10,389
ranges after merging (256 KB)      1,477  ->  1.15 GB of 3.64 GB
```

The fetch took 12 minutes at about 2.5 ranges per second and recovered exactly
the planned counts. Reconstruction produced 36,757 articles and 229 MB of prose;
the 2,009 that carry no transclusion are 1,792 redirects, 199 index and process
pages, and a handful written inline.

One thing the design note had wrong. Articles do not all use
`<pages from= to= />`. The commoner form by far is `include=`, which takes a
list that may be disjoint, because an article interrupted by a plate resumes
several pages later:

```
<pages index="EB1911 - Volume 27.djvu" include=687-688,690 fromsection="United States, The/History" />
<pages index="EB1911 - Volume 07.djvu" header="" include=635 onlysection=Cuesta />
<pages index="EB1911 - Volume 19.djvu" include=87-101 fromsection=Music tosection=s1 />
```

The first implementation understood only `from`/`to` and silently resolved 25,362
of 38,771 articles, reporting the other 11,415 as having no transclusion at all.
Supporting `include`, `exclude`, `step` and `onlysection` recovered them. The
lesson is the ordinary one: a parser that fails soft on a third of its input
looks exactly like a corpus that is a third smaller than you expected.

## What is in the NLS dataset

195 volumes, parsed by `src/common.py:parse_inventory_title`, all of them
matched:

| Edition | Era label | Volumes |
|---|---|---|
| 1st | 1771 | 6 |
| 2nd | 1784 | 10 |
| 3rd | 1797 | 36 |
| Supplement to the 3rd | 1801 | 4 |
| 4th | 1810 | 40 |
| 5th | 1817 | 20 |
| 6th | 1823 | 20 |
| Supplement to the 4th, 5th, 6th | 1824 | 15 |
| 7th | 1842 | 22 |
| 8th | 1860 | 22 |

Three title shapes had to be handled: a year that is present on some editions
and absent on others, `Volume 11 Part 1` in the fourth edition, and supplements
that name no edition of their own. The era label is the edition's completion
year, not the printing year, so the 1773 reprint of the first edition stays in
the 1771 layer.

## Licence

Items up to 1853 are public domain (CC Public Domain Mark 1.0); 1854 to 1860
carry a No Known Copyright statement. Wikisource and Wikipedia text is CC BY-SA.
Nothing in this repository redistributes the source corpora: `data/` is ignored
by git and every stage re-fetches from the original hosts.

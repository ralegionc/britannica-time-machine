"""Reading part of a Wikimedia multistream dump without downloading all of it.

A `pages-articles-multistream` dump is not one bzip2 file. It is thousands of
independent bzip2 streams laid end to end, 100 pages each, and Wikimedia
publishes an index of `offset:pageid:title` lines saying which stream every page
is in. That makes the dump seekable over HTTP: work out which streams hold the
pages you want, ask for those byte ranges, and decompress them on their own.

The saving depends entirely on how clustered the wanted pages are, and page id
order is roughly creation order, so the question is really whether the pages were
written at around the same time. EB1911 articles and their scan pages were
created in batches and occupy 10,389 of the Wikisource dump's 47,521 streams:
1.15 GB of 3.64 GB. The 35,805 modern Wikipedia articles they align to occupy
15,244 of 258,634 streams, 3.84 GB of 26.80 GB, which is better than it sounds
because articles an encyclopaedia of 1911 has an entry for tend to be old ones.
Pages genuinely scattered through a dump occupy nearly every stream and save
nothing. Price a plan before running it rather than assuming the trick pays.

Shared by `s03b` (Wikisource, the 1911 layer) and `s04b` (Wikipedia, the modern
layer).
"""
from __future__ import annotations

import bz2
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import requests

_PAGE_BLOCK = re.compile(r"<page>.*?</page>", re.DOTALL)


@dataclass
class Plan:
    """Byte ranges to fetch, and what they are expected to contain."""
    runs: list[tuple[int, int]]
    wanted_titles: int
    wanted_streams: int
    total_streams: int
    fetch_bytes: int
    dump_bytes: int

    @property
    def share(self) -> float:
        return self.fetch_bytes / self.dump_bytes if self.dump_bytes else 0.0

    def summary(self) -> str:
        return (f"{self.wanted_titles:,} titles in {self.wanted_streams:,} of "
                f"{self.total_streams:,} streams; {len(self.runs):,} ranges, "
                f"{self.fetch_bytes / 1e9:.2f} GB of {self.dump_bytes / 1e9:.2f} GB "
                f"({100 * self.share:.0f}%)")


def remote_size(sess: requests.Session, url: str) -> int:
    r = sess.head(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    return int(r.headers["Content-Length"])


def ensure_file(sess: requests.Session, url: str, dest: Path,
                log=None) -> Path:
    """Download a file once, resuming a partial copy rather than restarting."""
    total = remote_size(sess, url)
    have = dest.stat().st_size if dest.exists() else 0
    if have == total:
        if log:
            log.info("%s already present (%.0f MB)", dest.name, total / 1e6)
        return dest
    mode, headers = ("ab", {"Range": f"bytes={have}-"}) if have else ("wb", {})
    if log:
        log.info("downloading %s: %.0f MB%s", dest.name, (total - have) / 1e6,
                 f" (resuming at {have / 1e6:.0f} MB)" if have else "")
    with sess.get(url, headers=headers, stream=True, timeout=600) as r:
        r.raise_for_status()
        with dest.open(mode) as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    return dest


def iter_index(index_path: Path) -> Iterator[tuple[int, str]]:
    """Yield (stream offset, title) for every page in the index."""
    with bz2.open(index_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            off, _, rest = line.partition(":")
            _pageid, _, title = rest.partition(":")
            try:
                yield int(off), title.rstrip("\n")
            except ValueError:
                continue


def build_plan(index_path: Path, want: Callable[[str], bool], dump_size: int,
               merge_gap: int = 0) -> Plan:
    """Decide which byte ranges to fetch.

    Stream lengths are the gaps between distinct offsets, so every offset in the
    index has to be seen, not only the interesting ones. `merge_gap` joins ranges
    separated by less than that many bytes, trading wasted download for fewer
    requests: on the Wikisource dump, 256 KB turns 4,290 requests into 1,477 and
    costs 0.30 GB.
    """
    # Only distinct offsets are kept, never one entry per page: the Wikisource
    # index is 4.7M lines and English Wikipedia's is 23M, but they describe
    # 47,521 and a few hundred thousand streams respectively. Accumulating a
    # list of every line's offset costs hundreds of megabytes for nothing.
    distinct: set[int] = set()
    wanted: set[int] = set()
    n_titles = 0
    for off, title in iter_index(index_path):
        distinct.add(off)
        if want(title):
            n_titles += 1
            wanted.add(off)

    uniq = sorted(distinct)
    nxt = dict(zip(uniq, uniq[1:]))
    runs: list[list[int]] = []
    for off in sorted(wanted):
        end = nxt.get(off, dump_size)
        if runs and off - runs[-1][1] <= merge_gap:
            runs[-1][1] = end
        else:
            runs.append([off, end])
    return Plan(runs=[(a, b) for a, b in runs], wanted_titles=n_titles,
                wanted_streams=len(wanted), total_streams=len(uniq),
                fetch_bytes=sum(b - a for a, b in runs), dump_bytes=dump_size)


def fetch_range(sess: requests.Session, url: str, start: int, end: int) -> str:
    """Fetch one byte range and decompress it.

    A range covers whole streams and `bz2.decompress` handles a concatenation of
    them in one call. What comes back is a bare run of `<page>` elements with no
    document root, which is why `iter_pages` matches them out individually
    instead of parsing a document.
    """
    r = sess.get(url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=300)
    if r.status_code != 206:
        raise RuntimeError(f"range request refused with HTTP {r.status_code}")
    return bz2.decompress(r.content).decode("utf-8", "replace")


def iter_pages(xml_text: str) -> Iterator[tuple[str, str]]:
    """Yield (title, wikitext) for every page element in a decompressed chunk."""
    for block in _PAGE_BLOCK.finditer(xml_text):
        try:
            el = ET.fromstring(block.group(0))
        except ET.ParseError:
            continue
        title = (el.findtext("title") or "").strip()
        if not title:
            continue
        text_el = el.find("./revision/text")
        yield title, ((text_el.text if text_el is not None else None) or "")


def iter_stream_pages(sess: requests.Session, url: str, runs, log=None):
    """Fetch each range in turn and yield its pages, with the range index."""
    for i, (start, end) in enumerate(runs):
        try:
            text = fetch_range(sess, url, start, end)
        except OSError as e:  # truncated or misaligned stream
            if log:
                log.warning("range %d (%d-%d) did not decompress: %s", i, start, end, e)
            continue
        yield i, iter_pages(text)

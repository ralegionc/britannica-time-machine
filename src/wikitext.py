"""Turning Wikisource `Page:` wikitext into the prose an article is made of.

A transcluded EB1911 article is assembled from scan pages that look like this
(volume 6, page 278, the end of Choiseul and the start of Cholera):

    <noinclude><pagequality level="3" user="..." />{{rh|{{x-larger|262}}|...}}</noinclude>
    <section begin="s1" />duke's character, accuses him of exciting the war ...
    {{EB1911 fine print/s}}
    See ''Memoires du duc de Choiseul'', edited by F. Calmettes (Paris, 1904);
    {{EB1911 fine print/e}}
    <section end="s1" />
    <section begin="Cholera" />{{sc|Cholera}}, a term applied to ...

Three things have to be right, in this order:

1. **Include semantics.** `<noinclude>` is page furniture: running heads, folio
   numbers, proofreading state. It is what the reader of the article never sees,
   and it is exactly the material that would poison a retrieval index with
   headers. `<includeonly>` is the reverse, and `<onlyinclude>`, where present,
   overrides both.
2. **Sections.** Two articles share a printed page constantly, so the article
   tag names where to start and stop: `fromsection` on the first page,
   `tosection` on the last. Without this, every article absorbs its neighbours.
3. **Templates.** Dropping all of them loses real text ({{sc|Malignant Cholera}}
   is a sentence's subject); keeping all of them imports running heads. The rule
   below is explicit about which is which, and falls back on a judgement about
   whether a parameter looks like prose.

Hyphenation across a page break is carried by {{hws|dis|disease}} at the foot of
one page and {{hwe|ease|disease}} at the head of the next. Keeping the whole word
from `hws` and dropping `hwe` reconstructs it exactly once.
"""
from __future__ import annotations

import html
import re

# Templates whose content is prose and must survive, mapped to the positional
# parameter to keep. -1 means the last one.
KEEP_PARAM = {
    "sc": -1, "smallcaps": -1, "small-caps": -1, "uc": -1, "allcaps": -1,
    "italic": -1, "i": -1, "bold": -1, "b": -1, "nowrap": -1, "nobreak": -1,
    "larger": -1, "x-larger": -1, "smaller": -1, "x-smaller": -1,
    "lang": -1, "greek": -1, "polytonic": -1,
    "hws": 1,  # {{hws|part|whole}}: keep the whole word, drop the fragment
}

# Page furniture and markers with no readable content.
DROP = {
    "rh", "runningheader", "running header", "rvh", "hwe", "pagequality",
    "em", "gap", "dhr", "rule", "nop", "anchor", "anchor+", "ppoem",
    "smallrefs", "reflist", "c", "center", "right", "left", "img float",
    "sidenotes begin", "sidenotes end", "float right", "float left",
}
DROP_PREFIXES = ("eb1911 fine print", "block center", "smaller block")

_NOINCLUDE = re.compile(r"<noinclude\b[^>]*>.*?</noinclude>", re.DOTALL | re.IGNORECASE)
_NOINCLUDE_OPEN = re.compile(r"<noinclude\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
_INCLUDEONLY = re.compile(r"</?includeonly\b[^>]*>", re.IGNORECASE)
_ONLYINCLUDE = re.compile(r"<onlyinclude\b[^>]*>(.*?)</onlyinclude>", re.DOTALL | re.IGNORECASE)
_REF = re.compile(r"<ref\b[^>]*/>|<ref\b[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_TABLE = re.compile(r"\{\|.*?\|\}", re.DOTALL)
_HEADING = re.compile(r"^\s*={2,6}\s*(.*?)\s*={2,6}\s*$", re.MULTILINE)
_EXTLINK = re.compile(r"\[(?:https?:|//)\S+?(?:\s+([^\]]*))?\]")
_EMPH = re.compile(r"'{2,5}")
_MEDIA = re.compile(r"\[\[\s*(?:File|Image|Media|Category)\s*:", re.IGNORECASE)
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")
_SECTION = re.compile(
    r"""<section\s+(begin|end)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))\s*/?>""",
    re.IGNORECASE | re.DOTALL)

BEGIN, END = "begin", "end"


def section_markers(text: str) -> list[tuple[str, str, int, int]]:
    """Every section tag as (kind, label, start offset, end offset)."""
    out = []
    for m in _SECTION.finditer(text):
        label = m.group(2) or m.group(3) or m.group(4) or ""
        out.append((m.group(1).lower(), label.strip(), m.start(), m.end()))
    return out


def slice_section(text: str, from_label: str | None, to_label: str | None) -> str:
    """Cut a page down to one labelled section.

    `from_label` alone means the article starts here and runs to the foot of the
    page; `to_label` alone means it was already running and stops here; both
    means it begins and ends on this page. A label absent from the page is not an
    error, it means the article covers the whole page, which is the usual case
    for the middle pages of a long article.
    """
    marks = section_markers(text)
    start, end = 0, len(text)
    if from_label:
        for kind, label, _s, e in marks:
            if kind == BEGIN and label == from_label:
                start = e
                break
    if to_label:
        for kind, label, s, _e in marks:
            if kind == END and label == to_label and s >= start:
                end = s
                break
    return text[start:end]


def apply_include_rules(text: str) -> str:
    """Keep exactly what a transclusion would keep."""
    only = _ONLYINCLUDE.findall(text)
    if only:
        return "\n".join(only)
    text = _NOINCLUDE.sub("", text)
    text = _NOINCLUDE_OPEN.sub("", text)  # an unclosed tag at the end of a page
    return _INCLUDEONLY.sub("", text)


def split_params(body: str) -> list[str]:
    """Split a template body on a pipe at nesting depth zero."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth = max(0, depth - 1)
            cur.append(two)
            i += 2
            continue
        ch = body[i]
        if ch == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


_WORDY = re.compile(r"^[^\W\d_][\w'’.\- ]*$", re.UNICODE)


def _looks_like_prose(s: str) -> bool:
    """Whether an unlisted template's parameter is text rather than formatting.

    A sentence obviously qualifies. So does a bare cross-reference like
    `Palaeography`, which is most of what the EB1911 link templates carry, and
    dropping those was leaving holes in the middle of sentences: *are common in
    ancient writings and inscriptions (see and ), and very many...*. What must
    still be dropped is the formatting freight: `2.4em`, `4`, `left`, `#ccc`.
    """
    s = s.strip()
    if len(s) > 20 and " " in s:
        return True
    return len(s) >= 3 and bool(_WORDY.match(s)) and not s.lower() in {
        "left", "right", "center", "centre", "top", "bottom", "none", "yes", "no"}


def _expand_one(m: re.Match) -> str:
    parts = split_params(m.group(1))
    name = parts[0].strip().lower()
    args = [p.strip() for p in parts[1:]]
    positional = [a for a in args if "=" not in a[:20]]
    if name in DROP or name.startswith(DROP_PREFIXES):
        return ""
    if name in KEEP_PARAM and positional:
        try:
            return positional[KEEP_PARAM[name]]
        except IndexError:
            return positional[-1]
    if positional:
        longest = max(positional, key=len)
        if _looks_like_prose(longest):
            return longest
    return ""


def expand_templates(text: str) -> str:
    """Resolve templates innermost first, by the rules in KEEP_PARAM and DROP.

    An unlisted template keeps its longest parameter when that parameter looks
    like prose, and is dropped otherwise. That is the one place in this file
    where the output is a judgement rather than a rule: it keeps the text of
    wrappers nobody has enumerated, without importing the numeric and formatting
    noise that most unlisted templates consist of.
    """
    innermost = re.compile(r"\{\{([^{}]*)\}\}", re.DOTALL)
    for _ in range(12):  # nesting in this corpus is shallow
        new = innermost.sub(_expand_one, text)
        if new == text:
            return new
        text = new
    return innermost.sub("", text)


def strip_links(text: str) -> str:
    """`[[a|b]]` keeps b, `[[a]]` keeps a, files and categories go."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("[[", i):
            j, depth = i + 2, 1
            while j < len(text) and depth:
                if text.startswith("[[", j):
                    depth += 1
                    j += 2
                elif text.startswith("]]", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            inner = text[i + 2:j - 2]
            if not _MEDIA.match(text[i:i + 14]):
                out.append(split_params(inner)[-1])
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


_TABLE_LINE = re.compile(r"^\s*(\{\||\|\}|\|[-+]|!\s*$)")
_CELL_ATTRS = re.compile(r"^[^|\[\]{}]*=[^|]*\|(?!\|)")


def flatten_table(block: str) -> str:
    """Keep what a wikitable says, drop how it says it.

    Deleting tables outright cost the Abbreviation article 21 KB of its 23 KB:
    it is a table of abbreviations and their expansions, which is the article.
    Cell attributes (`style="..."`, `colspan=2`) are removed, cell contents are
    kept, one row per line.
    """
    rows = []
    for line in block.splitlines():
        if _TABLE_LINE.match(line):
            continue
        line = line.strip()
        if not line:
            continue
        if line[0] in "|!":
            line = line[1:]
        cells = [c for c in re.split(r"\|\||!!", line)]
        cleaned = [_CELL_ATTRS.sub("", c).strip() for c in cells]
        row = " ".join(c for c in cleaned if c)
        if row:
            rows.append(row)
    return "\n".join(rows)


def to_plain_text(wt: str) -> str:
    """Wikitext to the prose a reader would see, which is what gets indexed."""
    wt = _COMMENT.sub("", wt)
    wt = _REF.sub("", wt)
    wt = _TABLE.sub(lambda m: flatten_table(m.group(0)), wt)
    wt = expand_templates(wt)
    wt = strip_links(wt)
    wt = _EXTLINK.sub(lambda m: m.group(1) or "", wt)
    wt = _HEADING.sub(r"\1", wt)
    wt = _EMPH.sub("", wt)
    wt = _TAG.sub("", wt)
    wt = html.unescape(wt)
    wt = _WS.sub(" ", wt)
    wt = "\n".join(ln.strip() for ln in wt.splitlines())
    return _BLANKS.sub("\n\n", wt).strip()


_HEADER_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BAD_PARAM = re.compile(r"[{}<>\[\]|]")


def header_param(article_wikitext: str, name: str) -> str | None:
    """Read one parameter out of the `{{EB1911}}` header, or None.

    Guarded, because the obvious regex is wrong in a way that stays invisible
    until something downstream groups by the value. Forty-five articles carry
    `|wikipedia = {{import enwiki|...}}` and six carry the tail of an HTML
    comment; a reader that takes everything up to the next newline turns all
    fifty-one into one modern title, and the linker then merges forty-six
    unrelated biographies into a single subject.
    """
    text = _HEADER_COMMENT.sub("", article_wikitext or "")
    m = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n|}]*)", text)
    if not m:
        return None
    value = m.group(1).strip().strip("'\"")
    if not value or _BAD_PARAM.search(value) or value.startswith(("-", "=")):
        return None
    return value


_PAGES_TAG = re.compile(r"<pages\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
_INDEX_VOLUME = re.compile(r"EB1911\s*-\s*Volume\s*(\d+)\.djvu", re.IGNORECASE)


def parse_page_list(spec: str) -> list[int]:
    """Expand a ProofreadPage page list: `87-101`, `635`, `687-688,690`."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo <= hi:
                out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


def parse_pages_tag(article_wikitext: str) -> dict | None:
    """Read the `<pages ... />` transclusion of an article.

    Two forms are in use and both are common. `from`/`to` gives a contiguous
    range; `include` gives an explicit list that may be disjoint, because an
    article interrupted by a plate or a table of contents resumes several pages
    later (`include=687-688,690`). `exclude` subtracts and `step` thins.

    Sections come in two flavours as well. `fromsection`/`tosection` bound the
    first and last page, for an article that shares those pages with its
    neighbours. `onlysection` applies to every page, which is how an article
    that runs alongside another in parallel columns is separated.

    Returns None only when there is no transclusion at all: redirects, index
    pages, and the few articles written inline. Those are counted, not silently
    skipped.
    """
    m = _PAGES_TAG.search(article_wikitext)
    if not m:
        return None
    attrs = {}
    for a in _ATTR.finditer(m.group(1)):
        val = a.group(2) if a.group(2) is not None else (
            a.group(3) if a.group(3) is not None else a.group(4))
        attrs[a.group(1).lower()] = val.strip()
    vol = _INDEX_VOLUME.search(attrs.get("index", ""))
    if not vol:
        return None

    pages: list[int] = []
    if "include" in attrs:
        pages = parse_page_list(attrs["include"])
    elif "from" in attrs:
        try:
            lo = int(attrs["from"])
            hi = int(attrs.get("to", attrs["from"]))
        except ValueError:
            return None
        pages = list(range(lo, hi + 1)) if lo <= hi else []
    if not pages:
        return None

    if "exclude" in attrs:
        drop = set(parse_page_list(attrs["exclude"]))
        pages = [p for p in pages if p not in drop]
    try:
        step = int(attrs.get("step", 1))
    except ValueError:
        step = 1
    if step > 1:
        pages = pages[::step]

    # `section` is the older spelling of `onlysection`.
    only = attrs.get("onlysection") or attrs.get("section")
    return {
        "volume": int(vol.group(1)),
        "pages": pages,
        "from": pages[0],
        "to": pages[-1],
        "fromsection": attrs.get("fromsection"),
        "tosection": attrs.get("tosection"),
        "onlysection": only,
    }


_ENDS_HYPHENATED = re.compile(r"\{\{\s*hws\b[^{}]*\}\}\s*(?:<section\b[^>]*>\s*)*\Z",
                              re.IGNORECASE)


def assemble(spec: dict, page_text: dict[int, str]) -> tuple[str, int]:
    """Concatenate the scan pages of one article, honouring its section labels.

    Pages are joined with a newline, the way MediaWiki joins them, except at a
    seam where a word is split across the page break. There the `hws` template
    at the foot of one page has already contributed the whole word, so the next
    page has to follow with no separator at all or the word comes out cut in two.

    Returns the prose and the number of pages the spec asked for that were not in
    the dump, which is the metric that says whether a reconstruction is whole.
    """
    pages = spec.get("pages") or list(range(spec["from"], spec["to"] + 1))
    only = spec.get("onlysection")
    out, missing, seam = [], 0, False
    for i, n in enumerate(pages):
        raw = page_text.get(n)
        if raw is None:
            missing += 1
            continue
        body = apply_include_rules(raw)
        if only:
            body = slice_section(body, only, only)
        else:
            body = slice_section(
                body,
                spec["fromsection"] if i == 0 else None,
                spec["tosection"] if i == len(pages) - 1 else None,
            )
        if out:
            out.append("" if seam else "\n")
        out.append(body.lstrip() if seam else body)
        seam = bool(_ENDS_HYPHENATED.search(body.rstrip()))
    return to_plain_text("".join(out)), missing

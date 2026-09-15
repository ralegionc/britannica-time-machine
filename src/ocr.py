"""Normalising eighteenth-century OCR so that retrieval has a chance.

Three properties of the NLS text drive everything here, all three visible in the
first page of the 1771 volume 1:

1. The long s is scanned as `f`, and sometimes as `l`: *fcience*, *diffufe*,
   *profefled*. Left alone, every content word in the 1771 layer is out of
   vocabulary for any tokeniser or embedding model trained on modern English.
2. Line-end hyphenation is marked with `\u00ac` (and sometimes a plain hyphen):
   *fol\u00ac / lowing*, *re\u00ac / ferences*.
3. Text is one line per printed line, so paragraphs arrive pre-shredded.

The long-s restoration is lexicon-guided rather than rule-based: a token is only
rewritten when the original is absent from the lexicon and a substitution of it
is present. That makes the transform conservative by construction, and it means
`of`, `for`, `life` and `all` survive untouched. The lexicon is built from the
1911 layer (see s03), which is period-appropriate and long-s free.
"""
from __future__ import annotations

import re
from collections import Counter
from itertools import combinations

# Characters a long s is scanned as, in rough order of frequency.
SUSPECT = "fl"
MAX_SUSPECT_POSITIONS = 4

_WORD = re.compile(r"[A-Za-z\u00c6\u00e6\u0152\u0153']+")
_SOFT_HYPHEN_BREAK = re.compile(r"[\u00ac\u2010\u2011-]\s*\r?\n\s*")
_MULTISPACE = re.compile(r"[ \t]+")


def dehyphenate(text: str, lexicon: Counter | set | None = None) -> str:
    """Join words split across a line break.

    `\u00ac` is unambiguous and always joins. A plain hyphen is ambiguous, since
    *maiden-hair* is a real compound: the halves are rejoined without the hyphen
    only when the joined form is a word and the hyphenated form is not obviously
    a compound of two known words.
    """
    def _join(m: re.Match) -> str:
        return "\x00" if m.group(0)[0] == "\u00ac" else "\x01"

    marked = _SOFT_HYPHEN_BREAK.sub(_join, text)
    marked = marked.replace("\x00", "")
    if lexicon is None:
        return marked.replace("\x01", "")

    def _resolve(m: re.Match) -> str:
        left, right = m.group(1), m.group(2)
        if left.lower() in lexicon and right.lower() in lexicon:
            return f"{left}-{right}"
        return f"{left}{right}"

    return re.sub(r"([A-Za-z]+)\x01([A-Za-z]+)", _resolve, marked).replace("\x01", "")


def _candidates(token: str) -> list[str]:
    """Every way of reading suspect characters in `token` as a long s.

    A word-final `f` is never a long s (the long form was not used at the end of
    a word), which is what keeps *of*, *if* and *himself* intact.
    """
    positions = [i for i, ch in enumerate(token[:-1]) if ch in SUSPECT or ch in SUSPECT.upper()]
    if not positions or len(positions) > MAX_SUSPECT_POSITIONS:
        return []
    out = []
    for r in range(1, len(positions) + 1):
        for combo in combinations(positions, r):
            chars = list(token)
            for i in combo:
                chars[i] = "S" if chars[i].isupper() else "s"
            out.append("".join(chars))
    return out


def restore_token(token: str, lexicon) -> str:
    """Rewrite one token if, and only if, the lexicon says the rewrite is a word."""
    low = token.lower()
    if low in lexicon or not any(c in SUSPECT for c in low):
        return token
    best, best_score = None, -1.0
    for cand in _candidates(token):
        cl = cand.lower()
        if cl not in lexicon:
            continue
        freq = lexicon[cl] if hasattr(lexicon, "get") else 1
        # Prefer the commonest reading, then the one that changes least.
        score = freq - 0.001 * sum(a != b for a, b in zip(token, cand))
        if score > best_score:
            best, best_score = cand, score
    return best or token


def restore_long_s(text: str, lexicon) -> str:
    return _WORD.sub(lambda m: restore_token(m.group(0), lexicon), text)


def reflow(text: str) -> str:
    """Rejoin printed lines into paragraphs.

    A line break is treated as a paragraph break only when the next line starts a
    new entry (an all-caps headword) or the previous line ended a sentence and
    the next starts with a capital. Everything else is a wrap.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln:
            out.append("")
            continue
        if out and out[-1] and not _starts_entry(ln) and not _ends_block(out[-1]):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    return _MULTISPACE.sub(" ", "\n".join(out))


_ENTRY_HEAD = re.compile(r"^[A-Z\u00c6][A-Z\u00c6'\-]{2,}[,.;: ]")


def _starts_entry(line: str) -> bool:
    return bool(_ENTRY_HEAD.match(line))


def _ends_block(line: str) -> bool:
    return line.endswith((".", "?", "!")) and len(line) < 60


def tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text)]


def oov_counts(text: str, lexicon) -> tuple[int, int]:
    """(tokens the lexicon does not know, tokens in total)."""
    toks = tokens(text)
    return sum(1 for t in toks if t not in lexicon), len(toks)


def oov_rate(text: str, lexicon) -> float:
    """Share of tokens the lexicon does not know. The headline quality metric.

    Reported before and after normalisation in s05, per edition, because it is
    the only number that says whether the early layers are retrievable at all.
    Aggregate it over tokens rather than averaging per-entry rates: most entries
    in this corpus are two lines long, and a mean of rates lets a single bad
    token in a six-word entry weigh as much as a whole page of clean prose.
    """
    bad, total = oov_counts(text, lexicon)
    return bad / total if total else 0.0


def normalise(text: str, lexicon) -> str:
    """Full pipeline: dehyphenate, restore long s, reflow."""
    return reflow(restore_long_s(dehyphenate(text, lexicon), lexicon))

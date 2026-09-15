"""Shared paths, HTTP, rate limiting, logging, and the edition table."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW, INTERIM, PROC = DATA / "raw", DATA / "interim", DATA / "processed"
VOLUMES = RAW / "volumes"
OUT = ROOT / "outputs"
FIGS, TABLES = OUT / "figures", OUT / "tables"
NOTES = ROOT / "notes"
EVAL = ROOT / "eval"
for _p in (RAW, INTERIM, PROC, VOLUMES, FIGS, TABLES, NOTES):
    _p.mkdir(parents=True, exist_ok=True)

USER_AGENT = os.environ.get(
    "BTM_USER_AGENT",
    "britannica-time-machine/0.1 (open-source research project; contact via repository)",
)

NLS_TEXT_ZIP = "https://nlsfoundry.s3.amazonaws.com/text/nls-text-encyclopaediaBritannica.zip"
NLS_TRIAL_ZIP = "https://nlsfoundry.s3.amazonaws.com/trial/nls-trial-encyclopaediaBritannica.zip"
NLS_DATASET_PAGE = "https://data.nls.uk/data/digitised-collections/encyclopaedia-britannica/"
WIKISOURCE_API = "https://en.wikisource.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
EB1911_PREFIX = "1911 Encyclopædia Britannica/"

# The era axis. Each NLS edition is collapsed to a single year label, because a
# multi-volume edition published over a decade is still one snapshot of what the
# compilers believed. Years are the completion year of the edition.
EDITION_YEAR = {
    "first": 1771,
    "second": 1784,
    "third": 1797,
    "fourth": 1810,
    "fifth": 1817,
    "sixth": 1823,
    "seventh": 1842,
    "eighth": 1860,
    # The two supplements are dated to their completion year and kept on the era
    # axis: new subjects (steam, electromagnetism, geology) reached readers here
    # years before the next full edition absorbed them.
    "supplement-3": 1801,
    "supplement-4-6": 1824,
}
# Every era on the axis, in order. Ten editions and supplements from the NLS
# scans, the 1911 edition from Wikisource, and Wikipedia as it stands today.
ERAS = ["1771", "1784", "1797", "1801", "1810", "1817", "1823", "1824",
        "1842", "1860", "1911", "modern"]

ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4,
    "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    # Supplements sort between the editions they supplement.
    "supplement-3": 3.5, "supplement-4-6": 6.5,
}


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, per_minute: float):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    return s


def api_get(sess: requests.Session, url: str, params: dict,
            limiter: RateLimiter | None = None, tries: int = 5) -> dict:
    """GET a MediaWiki API endpoint as JSON, honouring maxlag and 429.

    Wikimedia asks automated clients to pass maxlag and to back off when the
    replicas fall behind, rather than to retry immediately.
    """
    params = {**params, "format": "json", "formatversion": 2, "maxlag": 5}
    delay = 1.0
    for attempt in range(tries):
        if limiter:
            limiter.wait()
        r = sess.get(url, params=params, timeout=60)
        if r.status_code in (429, 503):
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay *= 2
            continue
        r.raise_for_status()
        js = r.json()
        if js.get("error", {}).get("code") == "maxlag":
            time.sleep(delay)
            delay *= 2
            continue
        return js
    raise RuntimeError(f"giving up on {url} after {tries} tries: {params}")


_VOL_RE = re.compile(
    r"(?P<edition>First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth)\s+edition"
    r"(?:,\s*(?P<year>1[678]\d{2}))?"
    # "Volume 11 Part 1" and "Volume 18 Part I" both occur in the fourth edition.
    r"(?:,\s*Volume\s+(?P<volume>\d+|[IVXL]+)(?:\s+Part\s+(?P<part>\w+))?)?"
    r"(?:,\s*(?P<span>[^,]+?))?"
    r"\s*-\s*EB\.",
    re.IGNORECASE,
)

_SUPP_RE = re.compile(
    r"Supplement to the (?P<which>third|fourth, fifth and sixth) editions?"
    r".*?Volume\s+(?P<volume>\d+|[IVXL]+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_inventory_title(title: str) -> dict:
    """Pull edition, year, volume and letter span out of an NLS inventory title.

    The inventory is one line per volume, shaped like

        144133901.txt,"... - First edition, 1771, Volume 1, A-B - EB.1"
        193819047.txt,"Encyclopaedia Britannica - Eighth edition, Volume 21, T-ZWO - EB.16"

    The year is present on some editions and absent on others, so a missing year
    falls back to the edition completion year in EDITION_YEAR.
    """
    sup = _SUPP_RE.search(title)
    if sup:
        key = "supplement-3" if sup.group("which").lower() == "third" else "supplement-4-6"
        return {"edition": key, "edition_no": ORDINALS[key], "year": EDITION_YEAR[key],
                "printing_year": None, "volume": sup.group("volume"), "part": None,
                "span": None, "is_index": False, "is_supplement": True}

    m = _VOL_RE.search(title)
    if not m:
        return {"edition": None, "edition_no": None, "year": None,
                "printing_year": None, "volume": None, "part": None, "span": None,
                "is_index": "index" in title.lower(), "is_supplement": False}
    ed = m.group("edition").lower()
    span = (m.group("span") or "").strip() or None
    return {
        "edition": ed,
        "edition_no": ORDINALS[ed],
        # `year` is the era label and is always the edition's completion year:
        # the 1773 reprint of the first edition is still the 1771 snapshot.
        "year": EDITION_YEAR[ed],
        "printing_year": int(m.group("year")) if m.group("year") else None,
        "volume": m.group("volume"),
        "part": m.group("part"),
        "span": span,
        "is_index": bool(span and span.lower().startswith("index")) or "index" in title.lower(),
        "is_supplement": False,
    }


EB1911_DB = INTERIM / "eb1911.sqlite"


def eb1911_connect():
    """The 1911 article store, shared by the API stage (s03) and the dump (s03b).

    Both stages write the same rows, so downstream code never has to know which
    route a given article came from. `source` records it anyway, because the two
    are worth comparing and the comparison is how the dump route was validated.
    """
    import sqlite3

    con = sqlite3.connect(EB1911_DB, timeout=60)
    con.execute("""CREATE TABLE IF NOT EXISTS articles (
        title TEXT PRIMARY KEY, headword TEXT, volume TEXT, wikipedia TEXT,
        text TEXT, chars INTEGER, fetched REAL, source TEXT)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
    if "source" not in cols:  # table created before the dump route existed
        con.execute("ALTER TABLE articles ADD COLUMN source TEXT")
        con.execute("UPDATE articles SET source = 'api' WHERE source IS NULL")
        con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS ix_headword ON articles(headword)")
    # `articles` is keyed by title, so the dump route overwrites the API rows for
    # the same article. The snapshot keeps the API text alive for comparison,
    # which is the only way to check the offline reconstruction against what
    # MediaWiki itself renders.
    con.execute("""CREATE TABLE IF NOT EXISTS articles_api AS
                   SELECT * FROM articles WHERE 0""")
    return con

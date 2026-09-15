"""Probe every external source before any of the pipeline depends on it.

Writes outputs/tables/source_verification.csv. Re-run it whenever something
downstream fails: the answer is usually here.
"""
from __future__ import annotations

import csv
import sys

import requests

from common import (EB1911_PREFIX, NLS_DATASET_PAGE, NLS_TEXT_ZIP, TABLES,
                    WIKIPEDIA_API, WIKISOURCE_API, api_get, get_logger, session)

log = get_logger("s01")
CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str) -> None:
    CHECKS.append({"check": name, "ok": ok, "detail": detail})
    log.info("%s %-28s %s", "PASS" if ok else "FAIL", name, detail)


def check_nls_page(sess: requests.Session) -> None:
    try:
        r = sess.get(NLS_DATASET_PAGE, timeout=45)
        record("nls_dataset_page", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        record("nls_dataset_page", False, repr(e))


def check_nls_zip(sess: requests.Session) -> None:
    """Size, reachability, and the range support that s02 depends on."""
    try:
        r = sess.head(NLS_TEXT_ZIP, timeout=45, allow_redirects=True)
        size = int(r.headers.get("Content-Length", 0))
        record("nls_text_zip", r.status_code == 200 and size > 0,
               f"HTTP {r.status_code}, {size/1e6:.0f} MB")
    except Exception as e:  # noqa: BLE001
        record("nls_text_zip", False, repr(e))
        return
    try:
        r = sess.get(NLS_TEXT_ZIP, headers={"Range": "bytes=-1024"}, timeout=45)
        ok = r.status_code == 206 and len(r.content) == 1024
        record("nls_zip_range_requests", ok,
               f"HTTP {r.status_code}, {len(r.content)} bytes returned"
               + ("" if ok else "  (s02 must fall back to a full download)"))
    except Exception as e:  # noqa: BLE001
        record("nls_zip_range_requests", False, repr(e))


def check_wikisource(sess: requests.Session) -> None:
    try:
        js = api_get(sess, WIKISOURCE_API, {
            "action": "query", "list": "search", "srlimit": 1, "srinfo": "totalhits",
            "srsearch": f'prefix:"{EB1911_PREFIX}"'})
        hits = js["query"]["searchinfo"]["totalhits"]
        record("eb1911_article_count", hits > 30000, f"{hits:,} pages under the prefix")
    except Exception as e:  # noqa: BLE001
        record("eb1911_article_count", False, repr(e))
    try:
        js = api_get(sess, WIKISOURCE_API, {
            "action": "parse", "page": f"{EB1911_PREFIX}Cholera", "prop": "wikitext"})
        wt = js["parse"]["wikitext"]
        # The EB1911 template carries the volume and, usually, the modern
        # Wikipedia title. That parameter is the free half of the entry linking
        # in s06, so it is worth failing loudly if it disappears.
        record("eb1911_template_fields",
               "volume" in wt and "wikipedia" in wt,
               f"{len(wt)} chars of wikitext, volume and wikipedia params present")
        record("eb1911_page_transclusion", "<pages index=" in wt,
               "article body is transcluded from the Page: namespace, "
               "so prop=text is required to read it")
    except Exception as e:  # noqa: BLE001
        record("eb1911_template_fields", False, repr(e))


def check_wikipedia(sess: requests.Session) -> None:
    try:
        js = api_get(sess, WIKIPEDIA_API, {
            "action": "query", "prop": "extracts", "explaintext": 1,
            "titles": "Cholera", "redirects": 1})
        extract = js["query"]["pages"][0]["extract"]
        record("wikipedia_extracts", len(extract) > 2000, f"{len(extract):,} chars for Cholera")
    except Exception as e:  # noqa: BLE001
        record("wikipedia_extracts", False, repr(e))


def main() -> int:
    sess = session()
    check_nls_page(sess)
    check_nls_zip(sess)
    check_wikisource(sess)
    check_wikipedia(sess)

    out = TABLES / "source_verification.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["check", "ok", "detail"])
        w.writeheader()
        w.writerows(CHECKS)
    failed = [c for c in CHECKS if not c["ok"]]
    log.info("wrote %s (%d checks, %d failed)", out, len(CHECKS), len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

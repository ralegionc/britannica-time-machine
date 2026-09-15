"""Fetch NLS volume texts out of the Data Foundry zip, one volume at a time.

The published archive is 353 MB compressed and contains 194 plain text files,
one per volume, plus an inventory. Downloading all of it to read three editions
is wasteful, and S3 serves byte ranges, so this stage reads the zip's central
directory first and then pulls only the members it needs. A full download still
works and is what `--all` amounts to; there is just never a reason to hold the
whole archive on disk.

Resumable: every volume already on disk at the right size is skipped.
"""
from __future__ import annotations

import argparse
import csv
import json
import struct
import sys
import zlib

import requests

from common import (INTERIM, NLS_TEXT_ZIP, VOLUMES, get_logger,
                    parse_inventory_title, session)

log = get_logger("s02")
INDEX_CACHE = INTERIM / "nls_zip_index.json"
EOCD_SIG, CD_SIG, LOCAL_SIG = b"PK\x05\x06", b"PK\x01\x02", b"PK\x03\x04"
DEFAULT_EDITIONS = (1, 3, 6, 7, 8)


def _range(sess: requests.Session, start: int | None, length: int | None = None,
           suffix: int | None = None) -> bytes:
    if suffix is not None:
        header = f"bytes=-{suffix}"
    else:
        header = f"bytes={start}-{start + length - 1}"
    r = sess.get(NLS_TEXT_ZIP, headers={"Range": header}, timeout=180)
    r.raise_for_status()
    if r.status_code != 206:
        raise RuntimeError("server ignored the Range header; re-run with --all")
    return r.content


def read_index(sess: requests.Session, refresh: bool = False) -> dict:
    """Map member name -> (local header offset, compressed size, method)."""
    if INDEX_CACHE.exists() and not refresh:
        return json.loads(INDEX_CACHE.read_text(encoding="utf-8"))

    tail = _range(sess, None, suffix=66000)
    i = tail.rfind(EOCD_SIG)
    if i < 0:
        raise RuntimeError("no end-of-central-directory record in the last 66 KB")
    cd_size, cd_off = struct.unpack_from("<II", tail, i + 12)
    cd = _range(sess, cd_off, cd_size)

    index, pos = {}, 0
    while pos < len(cd) and cd[pos:pos + 4] == CD_SIG:
        method = struct.unpack_from("<H", cd, pos + 10)[0]
        csize, usize = struct.unpack_from("<II", cd, pos + 20)
        nlen, elen, clen = struct.unpack_from("<HHH", cd, pos + 28)
        offset = struct.unpack_from("<I", cd, pos + 42)[0]
        name = cd[pos + 46:pos + 46 + nlen].decode("utf-8", "replace")
        if not name.endswith("/"):
            index[name] = {"offset": offset, "csize": csize, "usize": usize,
                           "method": method}
        pos += 46 + nlen + elen + clen
    INDEX_CACHE.write_text(json.dumps(index, indent=1), encoding="utf-8")
    log.info("central directory: %d members", len(index))
    return index


def fetch_member(sess: requests.Session, name: str, meta: dict) -> bytes:
    blob = _range(sess, meta["offset"], meta["csize"] + 512)
    if blob[:4] != LOCAL_SIG:
        raise RuntimeError(f"no local header at offset {meta['offset']} for {name}")
    nlen, elen = struct.unpack_from("<HH", blob, 26)
    body = blob[30 + nlen + elen:30 + nlen + elen + meta["csize"]]
    raw = zlib.decompress(body, -15) if meta["method"] == 8 else body
    if len(raw) != meta["usize"]:
        raise RuntimeError(f"{name}: got {len(raw)} bytes, expected {meta['usize']}")
    return raw


def build_volume_table(inventory_csv: str) -> list[dict]:
    rows = []
    for rec in csv.reader(inventory_csv.splitlines()):
        if len(rec) < 2:
            continue
        fname, title = rec[0].strip(), rec[1].strip()
        if not fname.endswith(".txt"):
            continue
        meta = parse_inventory_title(title)
        rows.append({"file": fname, "volume_id": fname[:-4], "title": title, **meta})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--editions", default=",".join(map(str, DEFAULT_EDITIONS)),
                    help="comma-separated edition numbers to fetch (default: %(default)s)")
    ap.add_argument("--all", action="store_true", help="fetch every volume of every edition")
    ap.add_argument("--volumes", default="", help="comma-separated volume ids, overrides --editions")
    ap.add_argument("--limit", type=int, default=0, help="stop after N volumes (smoke tests)")
    ap.add_argument("--refresh-index", action="store_true")
    args = ap.parse_args()

    sess = session()
    index = read_index(sess, refresh=args.refresh_index)

    inv_name = next(n for n in index if n.endswith("inventory.csv"))
    inventory = fetch_member(sess, inv_name, index[inv_name]).decode("utf-8-sig", "replace")
    rows = build_volume_table(inventory)
    table = INTERIM / "volumes.csv"
    with table.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("inventory: %d volumes across editions %s -> %s", len(rows),
             sorted({r["edition_no"] for r in rows if r["edition_no"]}), table)

    if args.volumes:
        wanted = {v.strip() for v in args.volumes.split(",") if v.strip()}
        rows = [r for r in rows if r["volume_id"] in wanted]
    elif not args.all:
        keep = {int(e) for e in args.editions.split(",") if e.strip()}
        rows = [r for r in rows if r["edition_no"] in keep]
    rows = [r for r in rows if not r["is_index"]]
    if args.limit:
        rows = rows[:args.limit]

    log.info("fetching %d volumes (%.0f MB uncompressed)", len(rows),
             sum(index[f"{inv_name.rsplit('/', 1)[0]}/{r['file']}"]["usize"]
                 for r in rows) / 1e6)

    prefix = inv_name.rsplit("/", 1)[0]
    done = 0
    for r in rows:
        member = f"{prefix}/{r['file']}"
        dest = VOLUMES / r["file"]
        meta = index[member]
        if dest.exists() and dest.stat().st_size == meta["usize"]:
            continue
        raw = fetch_member(sess, member, meta)
        dest.write_bytes(raw)
        done += 1
        log.info("%-14s ed %s vol %-3s %-14s %6.1f MB", r["volume_id"],
                 r["edition_no"], r["volume"], r["span"], len(raw) / 1e6)
    log.info("%d volumes fetched, %d already present", done, len(rows) - done)
    return 0


if __name__ == "__main__":
    sys.exit(main())

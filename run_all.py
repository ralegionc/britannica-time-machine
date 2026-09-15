"""Run the pipeline end to end.

Every stage caches and resumes, so re-running is cheap.

Two stages have a bulk route and a one-at-a-time route, and the bulk one is the
default in both cases. `s03b` reconstructs the whole 1911 edition offline from
1.15 GB of byte ranges out of the Wikisource dump; `s04b` pulls the modern layer
50 titles per call. Their predecessors, `s03` and `s04`, fetch one article per
call against a quota that will not carry a whole layer, and survive for samples,
for top-ups, and because comparing the two routes is how the bulk ones were
checked.

  python run_all.py              # everything, dump route, default editions
  python run_all.py --quick      # API sample instead of the dump, no big download
  python run_all.py --only s02,s05
"""
import argparse
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"

STAGES = [
    ("s01_verify_sources.py", "probe every external source", [], []),
    ("s02_fetch_nls.py", "pull volume texts out of the NLS zip by byte range", [], []),
    ("s03b_fetch_eb1911_dump.py", "build the 1911 layer from the Wikisource dump",
     [], "SKIP"),
    ("s03_fetch_eb1911.py", "1911 layer from the API (sample only under --quick)",
     ["--lexicon-only"], ["--limit", "400"]),
    ("s04b_fetch_modern_bulk.py", "modern layer in bulk, 50 titles per call",
     [], ["--limit", "400"]),
    ("s05_segment_entries.py", "cut volumes into entries, normalise the OCR", [], []),
    ("s06_link_entries.py", "join each subject's entry across the twelve layers", [], []),
    ("s07_build_indexes.py", "chunk, build one FTS5 index per era, run the ablation",
     [], ["--queries-n", "60"]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma separated stage prefixes, e.g. s02,s05")
    ap.add_argument("--quick", action="store_true",
                    help="skip the dump download, take an API sample instead")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    for script, desc, full_args, quick_args in STAGES:
        if only and not any(script.startswith(p) for p in only):
            continue
        extra = quick_args if args.quick else full_args
        if extra == "SKIP":
            print(f"\n=== {script}  skipped (--quick) ===", flush=True)
            continue
        print(f"\n=== {script}  {desc} ===", flush=True)
        r = subprocess.run([sys.executable, str(SRC / script)] + list(extra))
        if r.returncode != 0:
            print(f"!! {script} exited {r.returncode}", file=sys.stderr)
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())

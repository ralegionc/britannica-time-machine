"""Segmentation tests against real text from NLS volume 144133901 (1771, A-B).

The fixture is a verbatim slice of the scan, so these assert against the
typography the project actually has to deal with, not a cleaned-up imitation.
"""
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import s05_segment_entries as seg  # noqa: E402

FIXTURE = """ADHERGAT, a town of Syria, near the frontiers of
Arabia.
ADHESION, implies the Ricking or adhering of two
bodies together.
Adhesion, in logic, fignifies tenacioufnefs to an argu¬
ment, without regard to any evidence of its truth.
ADHOA, in ancient cufloms. See Relief.
3
ADM (21
ADJACENT, an appellation given to fuch things as are
fituate near, or adjoining to each other.
VIII. to the number of 190, an yearly revenue of
"""

LEX = Counter({w: 50 for w in "argument the of in to a town near see relief".split()})


def entries():
    kept, stats = seg.segment(FIXTURE, LEX)
    return {e["headword"]: e for e in kept}, stats


def test_headwords_recovered():
    found, _ = entries()
    assert {"ADHERGAT", "ADHESION", "ADJACENT"} <= set(found)


def test_subsense_stays_inside_its_entry():
    # "Adhesion, in logic" is a sub-sense in small capitals, not a new entry.
    found, _ = entries()
    assert "in logic" in found["ADHESION"]["raw"]


def test_running_head_is_not_an_entry():
    found, stats = entries()
    assert "ADM" not in found
    assert stats["furniture_lines"] >= 1


def test_roman_numeral_line_is_not_a_headword():
    found, _ = entries()
    assert "VIII" not in found


def test_short_entries_are_dropped_and_counted():
    _, stats = entries()
    # ADHOA is two short lines and falls under the minimum body length.
    assert stats["too_short"] >= 1


@pytest.mark.skipif(not (ROOT / "data/raw/volumes/144133901.txt").exists(),
                    reason="run s02 --volumes 144133901 first")
def test_real_volume_yields_thousands_of_entries():
    text = (ROOT / "data/raw/volumes/144133901.txt").read_text(
        encoding="utf-8", errors="replace")
    kept, stats = seg.segment(text, LEX)
    assert len(kept) > 2000
    # A-B volume: the great majority of headwords should start with A or B.
    ab = sum(1 for e in kept if e["headword"][0] in "AB")
    assert ab / len(kept) > 0.8

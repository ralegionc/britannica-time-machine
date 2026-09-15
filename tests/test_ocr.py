"""Tests run against strings taken verbatim from NLS volume 144133901 (1771)."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ocr  # noqa: E402

LEX = Counter({w: 100 for w in """
science diffuse of for if himself all life following references professed
the to knowledge design work compilers subject maiden hair adhesion logic
sticking bodies together possession blessed
""".split()})


def test_long_s_restored():
    assert ocr.restore_token("fcience", LEX) == "science"
    assert ocr.restore_token("diffufe", LEX) == "diffuse"
    assert ocr.restore_token("himfelf", LEX) == "himself"
    assert ocr.restore_token("poffeffion", LEX) == "possession"


def test_final_f_is_never_a_long_s():
    for w in ("of", "if", "himself", "for"):
        assert ocr.restore_token(w, LEX) == w


def test_unknown_words_are_left_alone():
    # No substitution is a lexicon word, so the token survives unchanged rather
    # than being mangled into the nearest guess.
    assert ocr.restore_token("lubjedt", LEX) == "lubjedt"


def test_capitalisation_survives():
    assert ocr.restore_token("Fcience", LEX) == "Science"


def test_dehyphenation():
    assert "following" in ocr.dehyphenate("fol\u00ac\nlowing", LEX)
    assert ocr.dehyphenate("re\u00ac\nferences", LEX).strip() == "references"


def test_real_compound_keeps_its_hyphen():
    assert ocr.dehyphenate("maiden-\nhair", LEX).strip() == "maiden-hair"


def test_reflow_joins_wrapped_lines_but_splits_entries():
    raw = "ADHESION, implies the Ricking or adhering of two\nbodies together.\nADHOA, in ancient cufloms."
    out = ocr.reflow(raw).splitlines()
    assert len(out) == 2
    assert out[0].endswith("together.")


def test_oov_rate_drops_after_normalisation():
    raw = "the profefled defign of the fol\u00ac\nlowing work"
    before = ocr.oov_rate(raw, LEX)
    after = ocr.oov_rate(ocr.normalise(raw, LEX), LEX)
    assert after < before

"""Tests for the entry linker, written around the failures the audit found.

Every case here is one that actually went wrong on the real corpus before the
rule that prevents it existed: a chain through damaged spellings, two real words
one edit apart, a place declaring a neighbouring place as its own variant.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import s06_link_entries as s06  # noqa: E402


def test_norm_folds_typography_not_meaning():
    assert s06.norm("Cholera") == "CHOLERA"
    assert s06.norm("ABBEY-BOYLE") == "ABBEY BOYLE"
    assert s06.norm("Encyclopædia") == "ENCYCLOPAEDIA"
    assert s06.norm("Mémoires") == "MEMOIRES"
    assert s06.norm("Abbot, George") == "ABBOT GEORGE"
    assert s06.norm("  ") == ""


def test_within_one():
    assert s06.within_one("PINDUS", "PLNDUS")       # substitution
    assert s06.within_one("POTASH", "POT ASH")      # insertion
    assert s06.within_one("ALMORA", "ALMORAH")      # deletion
    assert s06.within_one("SAME", "SAME")
    assert s06.within_one("BARON", "BACON")         # one apart, and not one word
    assert not s06.within_one("BARON", "BEACON")    # two apart
    assert not s06.within_one("CAT", "HOUSE")


def test_dsu_tracks_headwords_and_refuses_to_overgrow():
    d = s06.DSU()
    a, b, c = "1771\tBAR", "1784\tBARR", "1797\tBARRE"
    d.union(a, b)
    assert not d.would_exceed(a, c, cap=3)
    assert d.would_exceed(a, c, cap=2)
    d.union(a, c)
    assert d.keys[d.find(a)] == {"BAR", "BARR", "BARRE"}


def test_fuzzy_maps_damage_onto_the_real_word():
    lex = {"pindus", "juniperus", "moorhen"}
    keys = {"PINDUS", "PLNDUS", "JUNIPERUS", "JUNIPERQS", "MOORHEN", "MOOR HEN"}
    mapping = s06.fuzzy_links(keys, lex)
    assert mapping["PLNDUS"] == "PINDUS"
    assert mapping["JUNIPERQS"] == "JUNIPERUS"
    assert mapping["MOOR HEN"] == "MOORHEN"
    assert "PINDUS" not in mapping  # a known word is never remapped


def test_two_real_words_one_edit_apart_are_left_alone():
    # The failure this rule exists for: a disease and a brachiopod, a baron and
    # a side of bacon. One edit is not a large distance in English.
    lex = {"anemia", "anomia", "baron", "bacon"}
    mapping = s06.fuzzy_links({"ANEMIA", "ANOMIA", "BARON", "BACON"}, lex)
    assert mapping == {}


def test_ambiguous_damage_is_rejected_rather_than_guessed():
    # PINDES is one edit from two known spellings. The evidence does not say
    # which was meant, so the route declines instead of picking one.
    lex = {"pindus", "pindas"}
    mapping = s06.fuzzy_links({"PINDES", "PINDUS", "PINDAS"}, lex)
    assert "PINDES" not in mapping


def test_short_keys_are_out_of_scope():
    lex = {"bolus"}
    assert s06.fuzzy_links({"BOLAS", "BOLUS"}, lex) == {}


def test_variant_declaration_is_read_only_at_the_head_of_an_entry():
    assert s06._VARIANT.match("or Anabaptiston, an obsolete term for a trepan.")
    assert s06._VARIANT.match("or Abele-tree, an obsolete name for a poplar.")
    # An "or" in the middle of prose is a disjunction, not a synonym.
    assert not s06._VARIANT.match("a town of Syria, or near the frontiers")
    # A lowercase continuation is not a name.
    assert not s06._VARIANT.match("or the same thing, in other words")

"""Tests against real Wikisource markup from EB1911 volume 6, pages 278-279.

The fragments are shortened but not cleaned up: the templates, section labels and
include tags are exactly as they appear in the scan pages that the Cholera
article transcludes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import wikitext as wt  # noqa: E402

PAGE_278 = (
    '<noinclude><pagequality level="3" user="DivermanAU" />'
    '{{rh|{{x-larger|262}}|{{x-larger|CHOISEUL-STAINVILLE—CHOLERA}}|{{em|2.4}}}}</noinclude>'
    '<section begin="s1" />tsarina Catherine II., and says of his foreign policy.\n'
    '{{EB1911 fine print/s}}\n'
    "See ''Mémoires du duc de Choiseul'', edited by F. Calmettes (Paris, 1904);\n"
    '{{EB1911 fine print/e}}\n'
    '<section end="s1" />\n'
    '<section begin="Cholera" />{{sc|Cholera}}, a term applied to a disease of the '
    'bowels, ending with ice and effervescing drinks which {{hws|serve|served}}'
    '<section end="Cholera" /><noinclude></noinclude>'
)

PAGE_279 = (
    '<noinclude><pagequality level="3" user="DivermanAU" />'
    '{{RunningHeader|{{em|2.4}}|{{x-larger|CHOLERA}}|{{x-larger|263}}}}</noinclude>\n'
    '{{hwe|d|served}} to quench the thirst and subdue the sickness.\n'
    "{{sc|Malignant Cholera}} (synonyms, ''Asiatic Cholera'') is one of the most "
    'severe diseases, described in [[Special:Search|the literature]].'
    '<noinclude></noinclude>'
)

ARTICLE = (
    '{{EB1911\n|title = [[../]]\n|volume = 6\n|previous = Choisy\n|next = Cholet\n'
    '|wikipedia = Cholera\n}}\n\n'
    '<pages index="EB1911 - Volume 06.djvu" header="" from="278" to="279" '
    'fromsection="Cholera" tosection="Cholera">\n</pages>\n\n'
    '[[Category:EB1911:Science:Biology|Cholera]]'
)


def test_noinclude_furniture_is_dropped():
    out = wt.to_plain_text(wt.apply_include_rules(PAGE_278))
    assert "262" not in out
    assert "CHOISEUL-STAINVILLE" not in out


def test_section_slicing_excludes_the_neighbouring_article():
    body = wt.slice_section(wt.apply_include_rules(PAGE_278), "Cholera", "Cholera")
    out = wt.to_plain_text(body)
    assert out.startswith("Cholera, a term applied")
    assert "tsarina" not in out
    assert "Calmettes" not in out


def test_small_caps_text_survives_but_running_heads_do_not():
    out = wt.to_plain_text(wt.apply_include_rules(PAGE_279))
    assert "Malignant Cholera" in out
    assert "263" not in out


def test_page_break_hyphenation_reconstructs_the_word_once():
    spec = wt.parse_pages_tag(ARTICLE)
    text, missing = wt.assemble(spec, {278: PAGE_278, 279: PAGE_279})
    assert missing == 0
    assert "served to quench" in text
    assert "serve served" not in text


def test_pages_tag_is_read_correctly():
    spec = wt.parse_pages_tag(ARTICLE)
    assert spec["volume"] == 6
    assert spec["pages"] == [278, 279]
    assert spec["fromsection"] == spec["tosection"] == "Cholera"
    assert spec["onlysection"] is None


def test_include_form_with_a_disjoint_page_list():
    # 1911 Encyclopaedia Britannica/United States, The/History, verbatim.
    spec = wt.parse_pages_tag(
        '<pages index="EB1911 - Volume 27.djvu" include=687-688,690 '
        'fromsection="United States, The/History" />')
    assert spec["pages"] == [687, 688, 690]
    assert spec["fromsection"] == "United States, The/History"


def test_onlysection_applies_to_every_page():
    spec = wt.parse_pages_tag(
        '<pages index="EB1911 - Volume 07.djvu" header="" include=635 onlysection=Cuesta />')
    assert spec["pages"] == [635]
    assert spec["onlysection"] == "Cuesta"


def test_unquoted_attributes_and_ranges():
    spec = wt.parse_pages_tag(
        '<pages index="EB1911 - Volume 19.djvu" include=87-101 fromsection=Music tosection=s1 />')
    assert spec["pages"][0] == 87 and spec["pages"][-1] == 101 and len(spec["pages"]) == 15
    assert spec["tosection"] == "s1"


def test_exclude_and_step():
    spec = wt.parse_pages_tag(
        '<pages index="EB1911 - Volume 01.djvu" include=10-14 exclude=12 />')
    assert spec["pages"] == [10, 11, 13, 14]


def test_onlysection_slices_every_page_it_is_given():
    page = ('<section begin="A" />first article text<section end="A" />'
            '<section begin="B" />second article text<section end="B" />')
    spec = {"volume": 1, "pages": [5, 6], "from": 5, "to": 6,
            "fromsection": None, "tosection": None, "onlysection": "B"}
    text, _missing = wt.assemble(spec, {5: page, 6: page})
    assert "first article" not in text
    assert text.count("second article text") == 2


def test_article_without_a_transclusion_returns_none():
    assert wt.parse_pages_tag("{{EB1911|volume=6}}\nplain wikitext, no pages tag") is None


def test_missing_scan_pages_are_counted():
    spec = wt.parse_pages_tag(ARTICLE)
    _text, missing = wt.assemble(spec, {278: PAGE_278})
    assert missing == 1


def test_links_keep_their_display_text():
    assert "the literature" in wt.to_plain_text(PAGE_279)
    assert "Special:Search" not in wt.to_plain_text(PAGE_279)


def test_italics_and_emphasis_markup_is_removed():
    out = wt.to_plain_text(wt.apply_include_rules(PAGE_279))
    assert "''" not in out
    assert "Asiatic Cholera" in out


def test_unlisted_template_keeps_a_prose_parameter():
    assert "a long sentence of real prose here" in wt.to_plain_text(
        "{{some unknown wrapper|a long sentence of real prose here}}")
    # but not a bare formatting argument
    assert wt.to_plain_text("{{unknown|2.4em}}") == ""


def test_table_contents_survive():
    table = (
        '{| class="wikitable"\n'
        '|-\n'
        '! Abbreviation !! Expansion\n'
        '|-\n'
        '| style="width:20%" | A.D. || Anno Domini\n'
        '|-\n'
        '| B.C. || Before Christ\n'
        '|}'
    )
    out = wt.to_plain_text(table)
    assert "A.D. Anno Domini" in out
    assert "B.C. Before Christ" in out
    assert "wikitable" not in out
    assert "width:20%" not in out


def test_short_cross_reference_in_an_unlisted_template_is_kept():
    out = wt.to_plain_text("inscriptions (see {{EB1911 link|Palaeography}}), and")
    assert "Palaeography" in out


def test_formatting_arguments_are_still_dropped():
    assert wt.to_plain_text("{{unknown|2.4em}}") == ""
    assert wt.to_plain_text("{{unknown|left}}") == ""

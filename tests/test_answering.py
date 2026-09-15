"""Tests for era-locked answering, abstention and anachronism detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import s08_answer as s08  # noqa: E402

PASSAGES = [
    {"doc_id": "1771:v:9", "headword": "CHOLERA MORBUS", "score": -8.1,
     "text": ("CHOLERA MORBUS, a violent purging and vomiting, arising from a "
              "redundancy of bile and a corrupt state of the air. The cure is "
              "attempted by opiates and by warm fomentations.")},
    {"doc_id": "1771:v:12", "headword": "BILE", "score": -6.0,
     "text": "BILE, a yellow bitter liquor separated in the liver."},
]


def test_content_words_drop_the_framing():
    assert s08.content_words("What causes cholera and how should it be treated?") == [
        "causes", "cholera", "treated"]


def test_coverage_is_the_share_of_question_terms_present():
    terms = ["causes", "cholera", "treated", "bacterium"]
    cov = s08.coverage(terms, PASSAGES)
    assert 0.2 < cov < 0.6           # cholera present, bacterium absent
    assert s08.coverage(terms, []) == 0.0


def test_extractive_answer_quotes_and_cites():
    answer, cites = s08.extractive_answer(["cholera", "bile", "cure"], PASSAGES)
    assert '"' in answer
    assert "[1771:v:9]" in answer
    assert cites and all(c.startswith("1771:") for c in cites)


def test_extractive_answer_cannot_invent_words():
    answer, _ = s08.extractive_answer(["cholera", "cure"], PASSAGES)
    source = " ".join(p["text"] for p in PASSAGES).lower()
    for word in s08.content_words(answer):
        assert word in source


def test_anachronism_flags_only_what_the_era_never_wrote():
    vocab = {"cholera", "morbus", "bile", "purging", "corrupt", "air", "opiates"}
    flags = s08.anachronisms(
        "Cholera is caused by the bacterium vibrio in contaminated water.",
        vocab, question_terms={"cholera", "caused"})
    assert "bacterium" in flags and "vibrio" in flags
    assert "cholera" not in flags     # it came from the question


def test_question_words_are_exempt_from_the_flagging():
    vocab = {"bile"}
    # "radioactivity" is in no 1771 vocabulary, but asking about it should not
    # count as the answer being anachronistic.
    flags = s08.anachronisms("radioactivity", vocab,
                             question_terms={"radioactivity"})
    assert flags == []


def test_refusal_string_is_stable():
    # s09 and s10 both match on it; changing it silently would break the diff.
    assert s08.REFUSAL == "NO ANSWER IN THIS EDITION"

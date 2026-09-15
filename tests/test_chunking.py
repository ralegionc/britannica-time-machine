"""Tests for chunking and query construction in s07."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import s07_build_indexes as s07  # noqa: E402

SHORT = "ADHESION, implies the sticking or adhering of two bodies together."
TREATISE = "The treatise on chemistry explains the nature of salts. " * 300


def test_a_short_entry_is_one_chunk():
    assert s07.split_text(SHORT) == [SHORT]


def test_a_treatise_is_split_and_nothing_is_lost():
    windows = s07.split_text(TREATISE)
    assert len(windows) > 5
    assert max(len(w) for w in windows) <= s07.MAX_CHUNK_CHARS
    # Overlapping windows cover more than the source, never less.
    assert sum(len(w) for w in windows) >= len(TREATISE)


def test_windows_start_at_a_word_boundary():
    for w in s07.split_text(TREATISE)[1:]:
        assert w[:1].isalnum()
        assert w.split(" ", 1)[0] in TREATISE.split()


def test_paragraph_boundaries_are_preferred():
    paras = "\n\n".join(f"Paragraph {i}. " + "word " * 70 for i in range(6))
    windows = s07.split_text(paras)
    assert len(windows) >= 2
    assert all(len(w) <= s07.MAX_CHUNK_CHARS for w in windows)


def test_headword_prefix_is_added_once():
    rows = list(s07.chunk_rows("1771", "norm", "d1", "ADHESION", SHORT))
    assert len(rows) == 1
    # Already begins with the headword, so nothing is prepended.
    assert rows[0][5] == SHORT
    later = list(s07.chunk_rows("1771", "norm", "d2", "CHEMISTRY", TREATISE))
    assert later[1][5].startswith("CHEMISTRY. ")


def test_query_drops_the_subject_name_and_the_function_words():
    q = s07.fts_query(
        "Cholera is an infection of the small intestine caused by the bacterium "
        "Vibrio cholerae.", {"cholera"})
    assert '"infection"' in q and '"bacterium"' in q
    # The subject's own name goes; the species name is part of the description
    # and stays, which is why the test looks for the exact token.
    assert '"cholera"' not in q
    assert '"cholerae"' in q
    assert '"the"' not in q and '"is"' not in q
    assert q.startswith('"') and " OR " in q


def test_query_terms_are_capped_and_deduplicated():
    # Distinct alphabetic words: the tokeniser takes letters only, so "term0"
    # and "term1" would both come back as "term".
    words = [f"aa{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(80)]
    q = s07.fts_query(" ".join(words), set())
    assert q.count(" OR ") == 24       # 25 terms
    twice = s07.fts_query("volcano volcano volcano eruption", set())
    assert twice == '"volcano" OR "eruption"'


def _tiny_index():
    """A two-era index in memory, with the same schema the real one uses."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE chunks (
        chunk_id INTEGER PRIMARY KEY, era TEXT, variant TEXT, doc_id TEXT,
        headword TEXT, seq INTEGER, text TEXT)""")
    rows = [
        ("1771", "norm", "1771:v:1", "CHOLERA", 0,
         "CHOLERA. a disorder arising from bile and a corrupt state of the air"),
        ("1911", "norm", "1911:Cholera", "Cholera", 0,
         "Cholera. caused by the bacterium vibrio cholerae in the intestine"),
    ]
    con.executemany("""INSERT INTO chunks (era, variant, doc_id, headword, seq, text)
                       VALUES (?,?,?,?,?,?)""", rows)
    con.commit()
    s07.build_indexes(con)
    return con


def test_an_era_index_cannot_reach_another_era():
    con = _tiny_index()
    # A term only the 1911 entry contains.
    assert s07.search(con, "1911", "norm", '"bacterium"') == ["1911:Cholera"]
    assert s07.search(con, "1771", "norm", '"bacterium"') == []
    # And the reverse: the 1771 wording is invisible to the 1911 index.
    assert s07.search(con, "1771", "norm", '"bile"') == ["1771:v:1"]
    assert s07.search(con, "1911", "norm", '"bile"') == []


def test_search_returns_documents_not_chunks():
    con = _tiny_index()
    con.executemany("""INSERT INTO chunks (era, variant, doc_id, headword, seq, text)
                       VALUES (?,?,?,?,?,?)""",
                    [("1911", "norm", "1911:Cholera", "Cholera", 1,
                      "Cholera. a second chunk of the same article about bacterium")])
    con.commit()
    s07.build_indexes(con)
    # Two chunks of one article collapse to one result.
    assert s07.search(con, "1911", "norm", '"bacterium"') == ["1911:Cholera"]

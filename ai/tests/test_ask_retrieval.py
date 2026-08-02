"""
The prefilter: which memos a question is about, and what the model is shown of them.

Two halves, and only one of them is testable here. **Whether the statements are
correct is not**: every one of them is Postgres -- `to_tsvector`, `ts_rank`,
`ts_headline`, a tsquery built with `quote_literal` -- and there is no in-memory
database that would run them honestly. That half was settled against a real Postgres
holding this schema, and the shape it produced is recorded at the top of
memo_ai/ask/retrieval.py.

What is testable, and is what these cover, is everything decided *around* the SQL:
that the parameters carry what the caller asked for, that the two empty cases stay
apart, and that an excerpt is capped where the prompt budget says it is.
"""

from datetime import UTC, datetime
from uuid import UUID

from memo_ai.ask import retrieval
from tests.support import FakeConnection

CREATED_AT = datetime(2026, 7, 31, 12, 0, 0, 123456, tzinfo=UTC)

MEMO_ID = UUID("01900000-0000-7000-8000-000000000001")


def row(**overrides) -> dict:
    fields = {
        "id": MEMO_ID,
        "title": "Call the dentist",
        "created_at": CREATED_AT,
        "transcript_chars": 77,
        "excerpt": "Remember to call the dentist about the appointment on Thursday morning.",
    }

    return fields | overrides


def connection(lexemes=("dentist",), rows=None) -> FakeConnection:
    """A connection answering the two statements `retrieve` runs, in order."""
    return FakeConnection(
        rows=[
            [(lexeme,) for lexeme in lexemes],
            [row()] if rows is None else rows,
        ]
    )


def test_the_question_is_reduced_to_lexemes_by_postgres_and_not_by_us():
    fake = connection()

    retrieval.retrieve(fake, "what did I say about the dentist", top_k=3, memo_chars=1200)

    # The whole question goes to `to_tsvector`, unedited. There is no stopword list
    # in this package and there must not be one: the index was built with the
    # `english` dictionary and only that dictionary knows what it dropped.
    assert fake.executed[0][1] == {"question": "what did I say about the dentist"}


def test_the_search_is_given_the_lexemes_the_first_statement_found():
    fake = connection(lexemes=("land", "page", "say"))

    retrieval.retrieve(fake, "what did I say about the landing page", top_k=3, memo_chars=1200)

    params = fake.params_for("ts_headline")

    # A bound text[], not an interpolated tsquery. `quote_literal` in the statement
    # is what turns these into tsquery syntax, so nothing a person typed reaches the
    # parser as an operator.
    assert params["lexemes"] == ["land", "page", "say"]


def test_top_k_and_the_headline_options_reach_the_statement():
    fake = connection()

    retrieval.retrieve(fake, "dentist", top_k=5, memo_chars=1200)

    params = fake.params_for("ts_headline")

    assert params["limit"] == 5
    assert params["headline"] == retrieval.HEADLINE_OPTIONS


def test_a_question_with_no_lexemes_never_reaches_the_search():
    """
    "what about it?" is all stopwords, so `to_tsvector` finds nothing to look for.

    The second statement is skipped entirely -- not run with an empty array, which
    would build the tsquery `''` and raise. That is the reason the two are separate
    statements rather than one query with a CTE.
    """
    fake = FakeConnection(rows=[[]])

    found = retrieval.retrieve(fake, "what about it?", top_k=3, memo_chars=1200)

    assert found.sources == ()
    assert found.terms == ()
    assert not found.has_terms
    assert len(fake.executed) == 1


def test_terms_that_match_nothing_are_a_different_empty_from_no_terms():
    """
    The distinction the caller acts on: "ask differently" against "you never said
    that". A single empty result covering both would send somebody rephrasing a
    question that was already fine.
    """
    fake = connection(lexemes=("kubernetes",), rows=[])

    found = retrieval.retrieve(fake, "what did I say about kubernetes", top_k=3, memo_chars=1200)

    assert found.sources == ()
    assert found.has_terms


def test_sources_are_numbered_from_one_in_the_order_the_database_returned_them():
    """
    The ref is assigned here and nowhere else. It is the citation handle -- the only
    thing the model is ever asked to reproduce -- so it has to be a small integer
    this process owns rather than anything the database or the model supplies.
    """
    fake = connection(
        rows=[
            row(title="First"),
            row(title="Second", id=UUID("01900000-0000-7000-8000-000000000002")),
        ]
    )

    found = retrieval.retrieve(fake, "dentist", top_k=3, memo_chars=1200)

    assert [source.ref for source in found.sources] == [1, 2]
    assert [source.title for source in found.sources] == ["First", "Second"]


def test_an_excerpt_shorter_than_the_cap_is_not_reported_as_truncated():
    fake = connection(rows=[row(excerpt="Short memo.", transcript_chars=11)])

    (source,) = retrieval.retrieve(fake, "memo", top_k=3, memo_chars=1200).sources

    assert source.excerpt == "Short memo."
    assert not source.truncated


def test_whitespace_is_collapsed_without_being_counted_as_truncation():
    """
    The statement returns the transcript's length *after* the same collapse, so a
    memo that only lost its line breaks is not reported as cut. Getting this wrong
    would put "read the memo" under every multi-line memo in the app.
    """
    # 15, which is what `length(btrim(regexp_replace(transcript, '\\s+', ' ', 'g')))`
    # answers for this text -- the same collapse `_excerpt` performs, which is the
    # point of the statement computing it rather than returning `length(transcript)`.
    fake = connection(rows=[row(excerpt="Two\n\nlines   here.", transcript_chars=15)])

    (source,) = retrieval.retrieve(fake, "lines", top_k=3, memo_chars=1200).sources

    assert source.excerpt == "Two lines here."
    assert not source.truncated


def test_an_excerpt_over_the_cap_is_cut_on_a_word_boundary_and_marked():
    fake = connection(rows=[row(excerpt="alpha bravo charlie delta", transcript_chars=25)])

    (source,) = retrieval.retrieve(fake, "bravo", top_k=3, memo_chars=15).sources

    assert source.excerpt == "alpha bravo …"
    assert source.truncated


def test_the_cap_includes_the_marker():
    """
    ``ASK_MEMO_CHARS`` is a prompt budget that memo_ai/ask/model.py sizes a context
    window from, so an excerpt that came back two characters over would make that
    arithmetic quietly wrong. Checked at several caps rather than one, because the
    off-by-two only appears where the cut lands.
    """
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliett"

    for limit in range(4, len(text) + 4):
        fake = connection(rows=[row(excerpt=text, transcript_chars=len(text))])

        (source,) = retrieval.retrieve(fake, "bravo", top_k=1, memo_chars=limit).sources

        assert len(source.excerpt) <= limit, (limit, source.excerpt)


def test_one_long_word_is_hard_cut_rather_than_returned_whole():
    """
    The input with no word boundary to respect, which is where `rpartition` earns
    its place over `rsplit` -- memo_ai/enrich/local.py's `_line` records the same
    off-by-one from the other side.
    """
    fake = connection(rows=[row(excerpt="a" * 100, transcript_chars=100)])

    (source,) = retrieval.retrieve(fake, "a", top_k=1, memo_chars=20).sources

    assert len(source.excerpt) == 20
    assert source.excerpt.endswith("…")


def test_a_blank_title_is_folded_to_none():
    """One spelling of "this memo has no title", for the prompt and for the UI."""
    fake = connection(rows=[row(title="   ")])

    (source,) = retrieval.retrieve(fake, "dentist", top_k=3, memo_chars=1200).sources

    assert source.title is None

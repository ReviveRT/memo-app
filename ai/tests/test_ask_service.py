"""
The order of an answer, and what happens when there is not one.

The event sequence is a contract -- `sources` first and exactly once, then text,
then exactly one of `done` or `error` -- because the client renders the memos it is
about to read about while the model is still processing the prompt, which on this
hardware is most of the wait. These tests are where that contract is pinned; the
app on top of them only turns each event into a line.
"""

from datetime import UTC, datetime
from uuid import UUID

from memo_ai.ask import service
from memo_ai.ask.model import ModelUnavailable
from tests.support import FakeConnection, RecordingAskModel

CREATED_AT = datetime(2026, 7, 31, 12, 0, 0, 123456, tzinfo=UTC)


def row(ref: int = 1, excerpt: str = "Call the dentist on Thursday.") -> dict:
    return {
        "id": UUID(f"01900000-0000-7000-8000-00000000000{ref}"),
        "title": f"Memo {ref}",
        "created_at": CREATED_AT,
        "transcript_chars": len(excerpt),
        "excerpt": excerpt,
    }


def connection(lexemes=("dentist",), rows=None) -> FakeConnection:
    return FakeConnection(
        rows=[
            [(lexeme,) for lexeme in lexemes],
            [row()] if rows is None else rows,
        ]
    )


# Whose memos are being answered from. This module is about the event sequence rather
# than about scoping -- tests/test_ask_retrieval.py covers that the value reaches the
# statement -- so every call here uses the same one.
OWNER = "01900000-0000-7000-8000-0000000000aa"


def answer(fake_connection, model, question="what about the dentist"):
    return list(
        service.answer(
            fake_connection,
            model,
            question,
            owner_id=OWNER,
            top_k=3,
            memo_chars=1200,
        )
    )


def types(events) -> list[str]:
    return [event.type for event in events]


def test_sources_come_first_then_the_text_then_done():
    events = answer(connection(), RecordingAskModel(chunks=("You ", "should call.")))

    assert types(events) == ["sources", "token", "token", "done"]


def test_the_sources_event_carries_what_the_model_was_shown():
    """
    The same excerpt, deliberately: a reader checking a citation should be checking
    what the answer was actually built from, not a different rendering of the memo.
    """
    events = answer(connection(rows=[row(1, "Call the dentist on Thursday.")]), RecordingAskModel())

    (source,) = events[0].payload["sources"]

    assert source["ref"] == 1
    assert source["excerpt"] == "Call the dentist on Thursday."
    assert source["id"] == "01900000-0000-7000-8000-000000000001"
    assert source["truncated"] is False


def test_timestamps_are_spelled_the_way_the_rest_of_the_api_spells_them():
    """
    Milliseconds and a literal Z, matching MemoRepository::COLUMNS. These reach the
    same client through the same proxy as every other timestamp in the app, and
    `isoformat()` would render `+00:00` instead.
    """
    events = answer(connection(), RecordingAskModel())

    assert events[0].payload["sources"][0]["created_at"] == "2026-07-31T12:00:00.123Z"


def test_a_question_with_no_terms_never_reaches_the_model():
    """
    Two fixed sentences beat twenty seconds of a 1.5B model producing an apology --
    and a model asked a question tends to answer it whether or not it was given
    anything to answer from.
    """
    model = RecordingAskModel()

    events = answer(FakeConnection(rows=[[]]), model, question="what about it?")

    assert types(events) == ["sources", "token", "done"]
    assert events[0].payload["sources"] == []
    assert events[1].payload["text"] == service.NO_TERMS
    assert model.calls == []


def test_terms_that_match_nothing_get_the_other_sentence():
    model = RecordingAskModel()

    events = answer(connection(lexemes=("kubernetes",), rows=[]), model)

    assert events[1].payload["text"] == service.NO_MATCHES
    assert model.calls == []


def test_the_citations_are_computed_from_the_whole_answer():
    """
    **A citation can be split across two chunks**, and often is -- "[" and "1]" are
    two tokens. Matching on the pieces as they go past would miss exactly the
    citations this feature exists to produce.
    """
    events = answer(
        connection(rows=[row(1), row(2)]),
        RecordingAskModel(chunks=("Call the dentist [", "1", "].")),
    )

    assert events[-1].type == "done"
    assert events[-1].payload["cited"] == [1]


def test_a_citation_to_a_memo_that_was_not_retrieved_is_not_reported():
    events = answer(
        connection(rows=[row(1)]),
        RecordingAskModel(chunks=("According to [7].",)),
    )

    assert events[-1].payload["cited"] == []


def test_a_failure_partway_through_arrives_as_the_last_event():
    """
    **The reason the stream carries an error type at all.** An HTTP status is chosen
    before the first byte and cannot be revised, so a generation that blows its
    deadline halfway through a sentence has already answered 200. The client shows
    what it has plus what went wrong.
    """
    events = answer(
        connection(),
        RecordingAskModel(
            chunks=("You should ",),
            error=ModelUnavailable("Generating an answer took too long and was stopped."),
        ),
    )

    assert types(events) == ["sources", "token", "error"]
    assert "took too long" in events[-1].payload["message"]


def test_an_error_replaces_done_rather_than_preceding_it():
    """
    Exactly one terminator. A client that saw both would have to decide which one
    won, and a client that treated `done` as authoritative would render a truncated
    answer as a complete one.
    """
    events = answer(
        connection(),
        RecordingAskModel(chunks=(), error=ModelUnavailable("busy")),
    )

    assert "done" not in types(events)


def test_the_model_is_shown_the_question_and_the_retrieved_memos():
    model = RecordingAskModel()

    answer(connection(rows=[row(1, "Call the dentist.")]), model, question="who do I call")

    assert "who do I call" in model.prompt
    assert "<<<BEGIN MEMO 1>>>" in model.prompt
    assert "Call the dentist." in model.prompt

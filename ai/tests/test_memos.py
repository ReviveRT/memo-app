"""
The parameters the two result writes are given, and what happens when the fence
loses.

Not the SQL. These statements are Postgres-specific to the point that no in-memory
substitute would run them honestly -- ``FOR UPDATE SKIP LOCKED``, ``now()``, a
fence on a microsecond ``timestamptz``. They were verified against a real Postgres,
and memo_ai/memos.py records what those runs showed. What is left here is the
marshalling around them, which is where a mistake would be silent rather than loud.
"""

from dataclasses import fields

from memo_ai.memos import _CLAIM_COLUMNS, MAX_LAST_ERROR_CHARS, ClaimedMemo, MemoQueue
from memo_ai.stt.base import Transcript
from tests.support import LOCKED_AT, FakeConnection, claimed_memo


def test_the_claim_projection_and_claimedmemo_name_the_same_columns():
    # The drift guard, moved forward from runtime to test time.
    #
    # psycopg's class_row passes every returned column to ClaimedMemo as a keyword
    # argument, so the two do already fail loudly on their own -- confirmed against
    # real Postgres: an extra `status` in the projection raises "got an unexpected
    # keyword argument 'status'", and dropping `audio_path` raises "missing 1
    # required positional argument: 'audio_path'". Both name the column. But that
    # happens on the first claim, in a worker container, which is a slower and more
    # confusing place to learn it than here.
    #
    # Sets rather than a list: class_row matches by name, so reordering the
    # projection is harmless and should not fail a test.
    projected = {column.strip() for column in _CLAIM_COLUMNS.split(",")}

    assert projected == {field.name for field in fields(ClaimedMemo)}


def test_the_fence_token_is_the_locked_at_that_was_claimed():
    # Handed straight back rather than re-read or recomputed. `now()` on the write
    # would fence against a value that is never equal to the one on the row, and
    # every result write would become a silent no-op.
    connection = FakeConnection(rowcount=1)
    memo = claimed_memo()

    MemoQueue(connection).finish_ready(memo, None)

    assert connection.last_params["locked_at"] == LOCKED_AT
    assert connection.last_params["id"] == memo.id
    assert "AND locked_at = %(locked_at)s" in connection.last_sql


def test_a_memo_with_no_new_transcript_passes_nulls_so_coalesce_keeps_the_row():
    # The text path. All three parameters NULL, and the COALESCE in the statement
    # is what turns that into "leave what is there" rather than "erase it".
    connection = FakeConnection(rowcount=1)

    MemoQueue(connection).finish_ready(claimed_memo(source="text"), None)

    assert connection.last_params["transcript"] is None
    assert connection.last_params["stt_provider"] is None
    assert connection.last_params["stt_model"] is None
    # Four now, since MEMO-13. A text memo has no audio and so no length.
    assert connection.last_params["duration_ms"] is None


def test_a_transcript_is_written_with_the_provider_that_actually_produced_it():
    # From the result, not from STT_PROVIDER. With MEMO-14's fallback in place the
    # two answers diverge, and `memos.stt_provider` wants the one that did the work.
    connection = FakeConnection(rowcount=1)
    transcript = Transcript(text="spoken words", provider="local", model="base")

    MemoQueue(connection).finish_ready(claimed_memo(), transcript)

    assert connection.last_params["transcript"] == "spoken words"
    assert connection.last_params["stt_provider"] == "local"
    assert connection.last_params["stt_model"] == "base"


def test_the_measured_duration_is_written_beside_the_transcript():
    connection = FakeConnection(rowcount=1)

    MemoQueue(connection).finish_ready(claimed_memo(), None, duration_ms=7_314)

    assert connection.last_params["duration_ms"] == 7_314
    assert "duration_ms = COALESCE" in connection.last_sql


def test_a_failure_can_carry_a_duration_and_coalesces_when_it_cannot():
    # MEMO-13's addition to the failure write. A memo refused for being too long is
    # refused *by* a duration, and the row would otherwise carry that sentence in
    # `last_error` beside a blank length. COALESCE because most failures never get
    # far enough to measure anything, and a bare assignment would then erase a
    # duration an earlier attempt had recorded.
    connection = FakeConnection(rowcount=1)
    queue = MemoQueue(connection)

    queue.fail(claimed_memo(), "This recording is 11:04 long...", duration_ms=664_200)

    assert connection.last_params["duration_ms"] == 664_200

    queue.fail(claimed_memo(), "Something else went wrong.")

    assert connection.last_params["duration_ms"] is None
    assert "duration_ms = COALESCE" in connection.last_sql


def test_a_write_that_matches_no_rows_reports_failure_rather_than_success():
    # An UPDATE matching nothing is a success as far as the driver is concerned, so
    # this is checked rather than assumed. Without it a worker that lost its claim
    # -- reaped as stuck and re-claimed elsewhere, from MEMO-16 -- would log a
    # completed job while the row said something else entirely.
    connection = FakeConnection(rowcount=0)

    assert MemoQueue(connection).finish_ready(claimed_memo(), None) is False
    assert MemoQueue(connection).fail(claimed_memo(), "went wrong") is False


def test_a_write_that_matches_one_row_reports_success():
    connection = FakeConnection(rowcount=1)

    assert MemoQueue(connection).finish_ready(claimed_memo(), None) is True
    assert MemoQueue(connection).fail(claimed_memo(), "went wrong") is True


def test_neither_result_write_touches_locked_at():
    # The token has to stay put for the whole life of the claim, or a second write
    # from the same job would fence against a value it had already moved.
    connection = FakeConnection(rowcount=1)
    queue = MemoQueue(connection)

    queue.finish_ready(claimed_memo(), None)
    queue.fail(claimed_memo(), "went wrong")

    for sql, _params in connection.executed:
        assert "SET status" in sql
        assert "locked_at =" not in sql.split("WHERE")[0]


def test_a_long_error_is_truncated_with_a_visible_marker():
    # `last_error` is returned on every row of GET /api/memos, and MEMO-17 builds a
    # failure UI on it. A provider returning a page of HTML should not end up in
    # each of those. The marker matters too: a message cut off mid-sentence with no
    # sign of it reads as a malformed error rather than a long one.
    connection = FakeConnection(rowcount=1)

    MemoQueue(connection).fail(claimed_memo(), "x" * (MAX_LAST_ERROR_CHARS + 100))

    written = connection.last_params["last_error"]

    assert len(written) == MAX_LAST_ERROR_CHARS
    assert written.endswith("…")


def test_an_error_that_fits_is_left_exactly_as_it_is():
    connection = FakeConnection(rowcount=1)

    MemoQueue(connection).fail(claimed_memo(), "The audio file could not be read.")

    assert connection.last_params["last_error"] == "The audio file could not be read."


def test_the_claim_projection_does_not_ship_the_tsvector():
    # `search_vector` is a STORED generated column and therefore part of
    # `RETURNING *`. This statement runs twice a second per replica whether or not
    # there is work, so shipping a full stemmed copy of every transcript on it is
    # the one thing the projection must not do -- the same rule
    # MemoRepository::COLUMNS states on the PHP side.
    connection = FakeConnection(rowcount=0, row=None)

    MemoQueue(connection).claim()

    assert "search_vector" not in connection.last_sql
    assert "RETURNING *" not in connection.last_sql


def test_claiming_an_empty_queue_is_none_rather_than_an_error():
    # The ordinary case: most polls find nothing.
    assert MemoQueue(FakeConnection(rowcount=0, row=None)).claim() is None

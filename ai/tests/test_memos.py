"""
The parameters the writes are given, which one runs, and what happens when the
fence loses.

Not the SQL. These statements are Postgres-specific to the point that no in-memory
substitute would run them honestly -- ``FOR UPDATE SKIP LOCKED``, ``now()``,
``make_interval``, a fence on a microsecond ``timestamptz``. They were verified
against a real Postgres, and memo_ai/memos.py records what those runs showed. What
is left here is the marshalling around them, which is where a mistake would be
silent rather than loud.
"""

from dataclasses import fields
from uuid import UUID

import pytest

from memo_ai.enrich import Enrichment
from memo_ai.memos import (
    _CLAIM_COLUMNS,
    FALLBACK_TITLE_CHARS,
    MAX_LAST_ERROR_CHARS,
    ClaimedMemo,
    MemoQueue,
    RetryPolicy,
)
from memo_ai.stt.base import Transcript
from tests.support import LOCKED_AT, POLICY, FakeConnection, claimed_memo


def queue(connection, policy: RetryPolicy = POLICY) -> MemoQueue:
    return MemoQueue(connection, policy)


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


def test_the_claim_projection_does_not_ship_the_tsvector():
    # `search_vector` is a STORED generated column and therefore part of
    # `RETURNING *`. This statement runs twice a second per replica whether or not
    # there is work, so shipping a full stemmed copy of every transcript on it is
    # the one thing the projection must not do -- the same rule
    # MemoRepository::COLUMNS states on the PHP side.
    connection = FakeConnection(rowcount=0, row=None)

    queue(connection).claim()

    assert "search_vector" not in connection.last_sql
    assert "RETURNING *" not in connection.last_sql


def test_claiming_an_empty_queue_is_none_rather_than_an_error():
    # The ordinary case: most polls find nothing.
    assert queue(FakeConnection(rowcount=0, row=None)).claim() is None


# ---------------------------------------------------------------------------
# Commit 1: the transcript
# ---------------------------------------------------------------------------


def test_the_fence_token_is_the_locked_at_that_was_claimed():
    # Handed straight back rather than re-read or recomputed. `now()` on the write
    # would fence against a value that is never equal to the one on the row, and
    # every result write would become a silent no-op.
    connection = FakeConnection(rowcount=1)
    memo = claimed_memo()

    queue(connection).commit_transcript(memo, Transcript(text="words", provider="local"))

    assert connection.last_params["locked_at"] == LOCKED_AT
    assert connection.last_params["id"] == memo.id
    assert "AND locked_at = %(locked_at)s" in connection.last_sql


def test_committing_a_transcript_does_not_move_the_row_out_of_processing():
    # The whole of the two-commit mechanism. Writing 'ready' here would publish a
    # memo with no title; `processing` is already true of a row between the two
    # commits, which is why the table needs no third status.
    connection = FakeConnection(rowcount=1)

    queue(connection).commit_transcript(claimed_memo(), Transcript(text="words", provider="local"))

    assert "status" not in connection.last_sql.split("WHERE")[0]


def test_a_transcript_is_written_with_the_provider_that_actually_produced_it():
    # From the result, not from STT_PROVIDER. With MEMO-14's fallback in place the
    # two answers diverge, and `memos.stt_provider` wants the one that did the work.
    connection = FakeConnection(rowcount=1)
    transcript = Transcript(text="spoken words", provider="local", model="base")

    queue(connection).commit_transcript(claimed_memo(), transcript)

    assert connection.last_params["transcript"] == "spoken words"
    assert connection.last_params["stt_provider"] == "local"
    assert connection.last_params["stt_model"] == "base"


def test_the_measured_duration_and_the_cost_ride_along_with_the_transcript():
    # Both COALESCEd, because both are frequently absent on a result that still has
    # a transcript, and a bare assignment would erase what an earlier attempt
    # measured. `cost_micro_usd` is None on every provider that exists today --
    # local transcription bills nobody -- and MEMO-22 is what starts reading it.
    connection = FakeConnection(rowcount=1)
    transcript = Transcript(text="words", provider="openai", model="whisper-1", cost_micro_usd=380)

    queue(connection).commit_transcript(claimed_memo(), transcript, duration_ms=7_314)

    assert connection.last_params["duration_ms"] == 7_314
    assert connection.last_params["cost_micro_usd"] == 380
    assert "duration_ms = COALESCE" in connection.last_sql
    assert "cost_micro_usd = COALESCE" in connection.last_sql


def test_committing_a_transcript_clears_the_error_a_previous_attempt_left():
    # `last_error` reaches the browser. A memo that failed twice and transcribed on
    # the third attempt would otherwise display the error it recovered from.
    connection = FakeConnection(rowcount=1)

    queue(connection).commit_transcript(claimed_memo(), Transcript(text="w", provider="local"))

    assert "last_error = NULL" in connection.last_sql


# ---------------------------------------------------------------------------
# Commit 2: ready, enriched or not
# ---------------------------------------------------------------------------


def test_finishing_with_no_enrichment_falls_back_to_the_transcript_for_a_title():
    # The shipped configuration, since no enricher is wired up until MEMO-21. All
    # four enrichment parameters NULL, and the COALESCE chain in the statement is
    # what turns that into a title cut from the transcript rather than no title.
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo())

    assert connection.last_params["title"] is None
    assert connection.last_params["summary"] is None
    assert connection.last_params["tags"] is None
    assert connection.last_params["category"] is None
    assert connection.last_params["title_chars"] == FALLBACK_TITLE_CHARS
    assert "split_part(transcript, chr(10), 1)" in connection.last_sql


def test_the_fallback_title_is_the_rule_the_frontend_already_uses():
    # web/src/memoLabel.js labels an untitled memo from its transcript: first line,
    # truncated to 60 with an ellipsis. A persisted title cut a different way would
    # not add a title to an untitled memo -- it would replace a label the user was
    # already seeing with a worse one.
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo())

    fallback = connection.last_sql

    assert "split_part(transcript, chr(10), 1)" in fallback
    assert "%(title_chars)s - 1" in fallback
    assert "'…'" in fallback


def test_an_enrichment_is_written_field_by_field_with_tags_as_a_list():
    # psycopg maps a Python list to `text[]`; the frozen tuple on the dataclass
    # would not, and neither would the empty list that an absent `tags` must not
    # become -- COALESCE reads NULL as "leave the column alone" and an empty array
    # as "set it to nothing".
    connection = FakeConnection(rowcount=1)
    enrichment = Enrichment(
        title="Order confirmation",
        summary="A short summary.",
        tags=("work", "orders"),
        category="task",
    )

    queue(connection).finish_ready(claimed_memo(), enrichment)

    assert connection.last_params["title"] == "Order confirmation"
    assert connection.last_params["summary"] == "A short summary."
    assert connection.last_params["tags"] == ["work", "orders"]
    assert connection.last_params["category"] == "task"
    assert connection.last_params["enriched"] is True


def test_an_enrichment_that_produced_nothing_does_not_stamp_enriched_at():
    # "An enricher ran" and "the memo was enriched" are different claims, and
    # `enriched_at` is the second one. MEMO-21 wants to be able to find the memos
    # that were not.
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo(), Enrichment())

    assert connection.last_params["enriched"] is False


def test_a_failed_enrichment_still_reaches_ready_and_records_why():
    # The rule the second commit exists to enforce: enrichment is best-effort and
    # may not fail a memo. `failed` means no transcript, and this row has one.
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo(), None, "The model returned nothing usable.")

    assert "SET status = 'ready'" in connection.last_sql
    assert connection.last_params["enrichment_error"] == "The model returned nothing usable."
    assert connection.last_params["enriched"] is False


def test_a_successful_enrichment_clears_the_previous_attempt_s_complaint():
    # The one column here that is assigned rather than COALESCEd, because its
    # absence is information. COALESCE would leave a ready, titled, summarised memo
    # still claiming enrichment had failed.
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo(), Enrichment(title="A title"))

    assert connection.last_params["enrichment_error"] is None
    assert "enrichment_error = %(enrichment_error)s" in connection.last_sql


def test_a_long_enrichment_error_is_truncated_like_a_transcription_one():
    connection = FakeConnection(rowcount=1)

    queue(connection).finish_ready(claimed_memo(), None, "x" * (MAX_LAST_ERROR_CHARS + 100))

    assert len(connection.last_params["enrichment_error"]) == MAX_LAST_ERROR_CHARS


# ---------------------------------------------------------------------------
# Failing and retrying
# ---------------------------------------------------------------------------


def test_a_retryable_failure_below_the_cap_goes_back_to_the_queue():
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(claimed_memo(attempts=1), "still loading", retryable=True)

    assert "SET status = 'queued'" in connection.last_sql
    assert connection.last_params["last_error"] == "still loading"
    assert connection.last_params["delay_seconds"] > 0


def test_a_retry_releases_the_fence_token_so_the_old_attempt_cannot_write():
    # The line that makes this more than `_FAIL` with a different status. A row
    # handed back to the queue still carrying the old token could be written by the
    # previous claim after a new worker had taken it.
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(claimed_memo(attempts=1), "went wrong", retryable=True)

    assert "locked_at = NULL" in connection.last_sql


def test_a_retryable_failure_at_the_cap_is_terminal():
    # Three attempts means the third one is the last, not that there are three
    # retries after it.
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(claimed_memo(attempts=3), "still loading", retryable=True)

    assert "SET status = 'failed'" in connection.last_sql


def test_a_failure_that_is_not_retryable_is_terminal_on_the_first_attempt():
    # A file ffmpeg cannot decode will not decode on the third attempt either, and
    # two more claims to confirm it cost 90 seconds and tell the user nothing.
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(claimed_memo(attempts=1), "unreadable", retryable=False)

    assert "SET status = 'failed'" in connection.last_sql
    assert connection.last_params["last_error"] == "unreadable"


def test_a_terminal_failure_can_carry_a_duration_and_coalesces_when_it_cannot():
    # MEMO-13's addition to the failure write. A memo refused for being too long is
    # refused *by* a duration, and the row would otherwise carry that sentence in
    # `last_error` beside a blank length. COALESCE because most failures never get
    # far enough to measure anything, and a bare assignment would then erase a
    # duration an earlier attempt had recorded.
    connection = FakeConnection(rowcount=1)
    memos = queue(connection)

    memos.fail_or_retry(
        claimed_memo(), "This recording is 11:04 long...", retryable=False, duration_ms=664_200
    )

    assert connection.last_params["duration_ms"] == 664_200

    memos.fail_or_retry(claimed_memo(), "Something else went wrong.", retryable=False)

    assert connection.last_params["duration_ms"] is None
    assert "duration_ms = COALESCE" in connection.last_sql


def test_a_long_error_is_truncated_with_a_visible_marker():
    # `last_error` is returned on every row of GET /api/memos, and MEMO-17 builds a
    # failure UI on it. A provider returning a page of HTML should not end up in
    # each of those. The marker matters too: a message cut off mid-sentence with no
    # sign of it reads as a malformed error rather than a long one.
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(
        claimed_memo(), "x" * (MAX_LAST_ERROR_CHARS + 100), retryable=False
    )

    written = connection.last_params["last_error"]

    assert len(written) == MAX_LAST_ERROR_CHARS
    assert written.endswith("…")


def test_an_error_that_fits_is_left_exactly_as_it_is():
    connection = FakeConnection(rowcount=1)

    queue(connection).fail_or_retry(
        claimed_memo(), "The audio file could not be read.", retryable=False
    )

    assert connection.last_params["last_error"] == "The audio file could not be read."


# ---------------------------------------------------------------------------
# The backoff curve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("attempts", "low", "high"), [(1, 24, 36), (2, 48, 72), (3, 96, 144)])
def test_the_backoff_doubles_per_attempt_and_stays_inside_its_jitter(attempts, low, high):
    # Base 30s, doubling, +/-20%. The bounds are the jitter window rather than a
    # single number, which is the only way to assert a jittered value without
    # seeding the module's random.
    delay = POLICY.delay_for(attempts)

    assert low <= delay <= high


def test_the_backoff_is_jittered_rather_than_fixed():
    # Two replicas that fail the same way at the same moment must not retry in
    # lockstep. Twenty draws collapsing to one value would mean the jitter is not
    # being applied at all.
    assert len({POLICY.delay_for(1) for _ in range(20)}) > 1


# ---------------------------------------------------------------------------
# Fencing
# ---------------------------------------------------------------------------


def test_a_write_that_matches_no_rows_reports_failure_rather_than_success():
    # An UPDATE matching nothing is a success as far as the driver is concerned, so
    # this is checked rather than assumed. Without it a worker that lost its claim
    # -- reaped as stuck and re-claimed elsewhere -- would log a completed job while
    # the row said something else entirely.
    lost = FakeConnection(rowcount=0)
    words = Transcript(text="w", provider="local")

    assert queue(lost).commit_transcript(claimed_memo(), words) is False
    assert queue(lost).finish_ready(claimed_memo()) is False
    assert queue(lost).fail_or_retry(claimed_memo(), "went wrong", retryable=False) is False
    assert queue(lost).fail_or_retry(claimed_memo(attempts=1), "wrong", retryable=True) is False


def test_a_write_that_matches_one_row_reports_success():
    held = FakeConnection(rowcount=1)
    words = Transcript(text="w", provider="local")

    assert queue(held).commit_transcript(claimed_memo(), words) is True
    assert queue(held).finish_ready(claimed_memo()) is True
    assert queue(held).fail_or_retry(claimed_memo(), "went wrong", retryable=False) is True


def test_neither_commit_point_touches_locked_at():
    # The token has to stay put across both commits, or the second write of a job
    # would fence against a value the first had already moved. The writes that *do*
    # release it are the ones that end the claim -- fail, retry, and the reaper.
    connection = FakeConnection(rowcount=1)
    memos = queue(connection)

    memos.commit_transcript(claimed_memo(), Transcript(text="w", provider="local"))
    memos.finish_ready(claimed_memo())

    for sql, _params in connection.executed:
        assert "locked_at =" not in sql.split("WHERE")[0]


def test_every_write_that_ends_a_claim_releases_the_token():
    connection = FakeConnection(rowcount=1)
    memos = queue(connection)

    memos.fail_or_retry(claimed_memo(attempts=3), "gave up", retryable=True)
    memos.fail_or_retry(claimed_memo(attempts=1), "try again", retryable=True)

    for sql, _params in connection.executed:
        assert "locked_at = NULL" in sql.split("WHERE")[0]


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------

REQUEUED = UUID("01900000-0000-7000-8000-0000000000a1")
ABANDONED = UUID("01900000-0000-7000-8000-0000000000a2")
SALVAGED = UUID("01900000-0000-7000-8000-0000000000a3")


def test_the_reaper_reports_each_outcome_separately():
    # Three statements, three answers, in the order `reap` runs them. They are kept
    # apart because the worker logs them at different levels: a requeue is routine,
    # an abandonment is an error, and a salvage lost nothing.
    connection = FakeConnection(rows=[[(REQUEUED,)], [(ABANDONED,)], [(SALVAGED,)]])

    reaped = queue(connection).reap()

    assert reaped.requeued == [REQUEUED]
    assert reaped.failed == [ABANDONED]
    assert reaped.salvaged == [SALVAGED]
    assert bool(reaped) is True


def test_a_reaper_pass_that_found_nothing_is_falsey():
    # The ordinary pass on a healthy stack, once a minute per replica. The worker
    # logs nothing for it.
    assert not queue(FakeConnection()).reap()


def test_every_reaper_statement_is_bounded_by_the_lease_and_the_attempt_cap():
    connection = FakeConnection()

    queue(connection).reap()

    assert len(connection.executed) == 3

    for sql, params in connection.executed:
        assert "status = 'processing'" in sql
        assert "locked_at < now() - make_interval(secs => %(lease_seconds)s)" in sql
        assert params["lease_seconds"] == POLICY.reap_after_seconds
        assert params["max_attempts"] == POLICY.max_attempts


def test_the_three_reaper_predicates_are_disjoint():
    # No row may be matched by two of them, which is what makes running the three
    # outside a transaction safe: a connection lost between statements leaves the
    # rest in `processing` for the next pass rather than half-resolved.
    connection = FakeConnection()

    queue(connection).reap()

    requeue, fail, salvage = (sql for sql, _params in connection.executed)

    assert "attempts < %(max_attempts)s" in requeue
    assert "attempts >= %(max_attempts)s" in fail and "transcript IS NULL" in fail
    assert "attempts >= %(max_attempts)s" in salvage and "transcript IS NOT NULL" in salvage


def test_the_reaper_gives_a_requeued_memo_a_backoff_computed_per_row():
    # One UPDATE resolves many rows and they have not all burned the same number of
    # claims, so the curve is in SQL rather than in `delay_for`. `random()` inside
    # the statement is evaluated per row, so two memos reaped together do not come
    # back in the same instant.
    connection = FakeConnection()

    queue(connection).reap()

    sql, params = connection.executed[0]

    assert params["backoff_seconds"] == POLICY.backoff_seconds
    assert "power(2, greatest(attempts - 1, 0))" in sql
    assert "0.8 + random() * 0.4" in sql


def test_a_reaped_memo_with_a_transcript_is_published_rather_than_failed():
    # `failed` means no transcript. A memo killed in the gap between the two
    # commits, three times over, has its text safely on the row -- sending that to
    # `failed` would both break the invariant and hide a completed transcription
    # behind an error badge.
    connection = FakeConnection()

    queue(connection).reap()

    salvage, params = connection.executed[2]

    assert "SET status = 'ready'" in salvage
    assert "transcript IS NOT NULL" in salvage
    assert "last_error = NULL" in salvage
    assert params["title_chars"] == FALLBACK_TITLE_CHARS
    assert params["enrichment_error"]


def test_the_reaper_never_overwrites_an_error_the_job_itself_recorded():
    # COALESCE on both terminal messages. A memo that failed with a real reason and
    # was then reaped should keep the reason, not be relabelled "interrupted".
    connection = FakeConnection()

    queue(connection).reap()

    _requeue, fail, salvage = (sql for sql, _params in connection.executed)

    assert "last_error = COALESCE(last_error, %(last_error)s)" in fail
    assert "enrichment_error = COALESCE(enrichment_error, %(enrichment_error)s)" in salvage

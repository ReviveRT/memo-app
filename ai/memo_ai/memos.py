"""
SQL for the ``memos`` table, from the worker's side. Nothing above this module
writes a statement, the same way nothing above ``App\\Repositories`` does on the
PHP side.

There is no jobs table. Queue state (``status``, ``attempts``, ``locked_at``,
``next_attempt_at``) lives on the memo row, so a memo and the work it owes are
created by one INSERT and there is nothing to reconcile after a crash --
db/migrations/001_init.sql has the reasoning.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import class_row

from memo_ai.stt.base import Transcript

log = logging.getLogger(__name__)

# `last_error` is returned to the browser by GET /api/memos, and MEMO-17 builds a
# failure UI on top of it. A cap here keeps a pathological message -- a driver
# error carrying a query, a provider returning a page of HTML -- out of every row
# of that response. 500 characters is more than any sentence a person needs and
# less than anything worth paginating.
MAX_LAST_ERROR_CHARS = 500


@dataclass(frozen=True)
class ClaimedMemo:
    """
    One claimed row, holding only what the pipeline reads.

    The field names are the contract with :data:`_CLAIM_COLUMNS` below, and it is
    enforced rather than documented: the cursor uses psycopg's ``class_row``, which
    passes every returned column to this constructor as a keyword argument. A
    column added to the projection and not to this class raises ``TypeError:
    unexpected keyword argument`` on the first claim, and a field renamed on this
    side raises ``missing 1 required positional argument``. Both are loud and both
    name the column. That is the same guard ``Memo::REQUIRED_COLUMNS`` provides in
    PHP, for free.
    """

    id: UUID
    source: str
    transcript: str | None
    audio_path: str | None
    attempts: int

    # The fence token. Read here so it can be handed straight back to the result
    # write -- see :meth:`MemoQueue.finish_ready`. `timestamptz` and Python's
    # `datetime` are both microsecond-precision, so the round trip is exact; the
    # equality in the WHERE clause was checked against a value that had been out to
    # Python and back rather than assumed from that.
    locked_at: datetime


# Enumerated, not `RETURNING *`, and this table makes that a rule rather than a
# preference -- `MemoRepository::COLUMNS` states the same one on the PHP side.
#
# `search_vector` is a STORED generated column, so it is part of `*`: confirmed
# against this schema, where a 22-character transcript already produced 58
# characters of tsvector. Every claim would drag a full stemmed copy of the
# transcript over the wire, on the one statement that runs twice a second per
# replica whether or not there is any work.
#
# The other half is that `class_row` above would then need a field per column,
# including the tsvector, and adding a column to the table would break the worker.
_CLAIM_COLUMNS = "id, source, transcript, audio_path, attempts, locked_at"

# The claim. One statement, and it must stay one statement.
#
# `FOR UPDATE SKIP LOCKED` in the subquery is what makes two replicas safe: the
# row is locked as it is selected, and a second claimer skips it instead of
# queueing behind it. Verified with two concurrent claimers against two queued
# rows -- they took one each, no overlap, and a claimer that found the only queued
# row already locked returned zero rows in 0.13s rather than blocking.
#
# `ORDER BY created_at` makes this approximately FIFO, not strictly: whichever
# claimer reaches the oldest row first takes it and the other skips to the next.
# Observed in that same run, and it is the right trade -- strict ordering across
# replicas would mean serialising the claim, which is the thing `replicas: 2`
# exists to avoid.
#
# `attempts = attempts + 1` is here, in the claim, rather than in the result
# write. That is what makes the count survive a `SIGKILL` mid-work, which is what
# lets MEMO-16 terminate a poison memo at 3 instead of retrying it forever.
#
# `updated_at` is deliberately absent: db/migrations/002_updated_at.sql installs a
# BEFORE UPDATE trigger precisely so that this statement -- written in a different
# language by an author with no reason to read the PHP -- cannot forget it.
# Confirmed on this schema: running this claim moves `updated_at`.
_CLAIM = f"""
    UPDATE memos
       SET status = 'processing',
           locked_at = now(),
           attempts = attempts + 1
     WHERE id = (
               SELECT id
                 FROM memos
                WHERE status = 'queued'
                  AND next_attempt_at <= now()
                ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                LIMIT 1
           )
    RETURNING {_CLAIM_COLUMNS}
"""

# The success write. `status='ready'` and whatever the job produced.
#
# COALESCE on all three, so this one statement serves both kinds of memo. A text
# memo arrives with its transcript already set and owes no transcription
# (MEMO-06), so the pipeline hands back no `Transcript` and all three parameters
# are NULL -- COALESCE keeps what is on the row. A voice memo overwrites them.
#
# The useful side effect is that this statement *cannot* null a transcript out.
# MEMO-16's goal is that a transcript is never lost, and this is the shape that
# makes losing one require editing the SQL rather than passing the wrong argument.
#
# No `next_attempt_at` reset and no `last_error` clear: `ready` is terminal, and
# MEMO-16 owns the retry bookkeeping that would need either.
_FINISH_READY = """
    UPDATE memos
       SET status = 'ready',
           transcript = COALESCE(%(transcript)s, transcript),
           stt_provider = COALESCE(%(stt_provider)s, stt_provider),
           stt_model = COALESCE(%(stt_model)s, stt_model)
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""

# The failure write.
#
# `failed` with no retry, which is MEMO-08's whole failure policy and is smaller
# than the one MEMO-16 ships: three attempts with exponential backoff and jitter
# written to `next_attempt_at`, reaching `failed` only on the last one. The reason
# to write a terminal state now rather than leave the row alone is that `processing`
# is not re-claimable -- the claim predicate is `status='queued'` -- so a job that
# failed and wrote nothing would sit in `processing` forever with no `last_error`
# to explain it, and no reaper yet to notice. A visible dead end beats an invisible
# one.
#
# Consistent with MEMO-16's rule that `failed` means "no transcript": the only
# failure this task can produce is a transcription failure.
_FAIL = """
    UPDATE memos
       SET status = 'failed',
           last_error = %(last_error)s
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""


class MemoQueue:
    """
    The three statements above, over one connection.

    A class rather than module functions, and a thin one, for the reason
    ``MemoRepository`` is not final on the PHP side: it is the seam the pipeline
    tests substitute. Every statement here is Postgres-specific -- ``FOR UPDATE
    SKIP LOCKED``, ``now()``, a fence on a ``timestamptz`` -- so the alternative to
    a seam is either a second definition of the schema in the test suite or no unit
    tests of the pipeline at all. What the substitution cannot cover is whether
    these statements are *correct*, which is why the claim and the fence were
    checked against a real Postgres instead.

    The connection is expected to be in autocommit -- see memo_ai/db.py for why
    that is load-bearing rather than incidental.
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def claim(self) -> ClaimedMemo | None:
        """
        Take the oldest due memo, or ``None`` when there is nothing to do.

        ``None`` is the ordinary case, not an error: this runs on every poll and
        most polls find an empty queue.
        """
        with self._connection.cursor(row_factory=class_row(ClaimedMemo)) as cursor:
            cursor.execute(_CLAIM)

            return cursor.fetchone()

    def finish_ready(self, memo: ClaimedMemo, transcript: Transcript | None) -> bool:
        """Commit the result and move the row to ``ready``. False if the fence lost."""
        return self._fenced(
            _FINISH_READY,
            {
                "id": memo.id,
                "locked_at": memo.locked_at,
                "transcript": None if transcript is None else transcript.text,
                "stt_provider": None if transcript is None else transcript.provider,
                "stt_model": None if transcript is None else transcript.model,
            },
            memo,
            "finish",
        )

    def fail(self, memo: ClaimedMemo, error: str) -> bool:
        """Record a terminal failure. False if the fence lost."""
        return self._fenced(
            _FAIL,
            {
                "id": memo.id,
                "locked_at": memo.locked_at,
                "last_error": _truncate(error),
            },
            memo,
            "fail",
        )

    def _fenced(self, sql: str, params: dict, memo: ClaimedMemo, what: str) -> bool:
        """
        Run a write fenced on ``locked_at`` and report whether it landed.

        Checking ``rowcount`` is the point of this method. The fence makes the
        statement a legal no-op whenever this worker is no longer the owner of the
        claim, and an UPDATE that matched nothing is a success as far as the driver
        is concerned -- so without this check a worker that lost the row would log a
        completed job while the row said something else entirely.

        That is not a hypothetical once MEMO-16's reaper exists: a job reaped as
        stuck is re-claimed with a *new* ``locked_at``, and the original -- still
        running, because a reaped job is not a stopped one -- must not be able to
        overwrite the new attempt. Fencing is also why the two writes above never
        touch ``locked_at``: the token has to stay put for the claim's whole life.

        Warning rather than raising: losing the fence is a correct outcome of a
        correct design, and the row is already in the hands of whoever holds the
        claim now. There is nothing for this worker to fix and nothing to retry.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(sql, params)
            matched = cursor.rowcount

        if matched != 1:
            log.warning(
                "memo %s: %s write matched %d rows, not 1 -- the claim taken at %s "
                "is no longer ours, so another worker owns this memo now",
                memo.id,
                what,
                matched,
                memo.locked_at.isoformat(),
            )

            return False

        return True


def _truncate(error: str) -> str:
    if len(error) <= MAX_LAST_ERROR_CHARS:
        return error

    # The marker matters: a truncated message that ends mid-sentence with no sign
    # of it reads like the error itself was malformed.
    return error[: MAX_LAST_ERROR_CHARS - 1] + "…"

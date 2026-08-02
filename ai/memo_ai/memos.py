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
import random
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import class_row

from memo_ai import failures, titles
from memo_ai.config import Settings
from memo_ai.enrich import Enrichment
from memo_ai.stt.base import Transcript

log = logging.getLogger(__name__)

# `last_error` is returned to the browser by GET /api/memos, and MEMO-17 builds a
# failure UI on top of it. A cap here keeps a pathological message -- a driver
# error carrying a query, a provider returning a page of HTML -- out of every row
# of that response. 500 characters is more than any sentence a person needs and
# less than anything worth paginating.
MAX_LAST_ERROR_CHARS = 500

# How much of the transcript becomes the title when nothing better exists.
#
# MEMO-16's rule is that a memo is never untitled, and this is the whole of the
# mechanism. It is applied in SQL rather than in Python, which is the part worth
# knowing: the fallback has to work on a job that produced no transcript *this
# time* -- a memo whose transcription committed on an earlier attempt and whose
# enrichment is only now finishing has its text on the row and nowhere else.
FALLBACK_TITLE_CHARS = 60

# The fallback itself, as one expression, so the two statements that need it cannot
# drift apart. Built as a fragment rather than repeated, the same way `_CLAIM` is
# built from `_CLAIM_COLUMNS`.
#
# The task specifies "the first 60 characters of the transcript" and this is a
# little more than that, deliberately, because the app already has this rule and a
# second one that disagreed would be visible. web/src/memoLabel.js labels a memo
# with its title, and until one exists it derives a label from the transcript --
# first *line*, truncated to 60 with an ellipsis. So a persisted title cut a
# different way would not add a title to an untitled memo, it would replace a label
# the user was already seeing with a worse one: a typed memo opening with its own
# heading reads "Sunday Meeting" today and would read "Sunday Meeting We discussed
# the budget and then we mov" once this column is filled in.
#
# Hence the three parts, each matching that function:
#
#   * the first *line* of the trimmed transcript. A typed memo often opens with a
#     heading, and cutting mid-sentence when a natural break was two words earlier
#     makes the strip look like it is guessing. `chr(10)` rather than an escaped
#     literal, so what this file contains and what Postgres parses cannot differ
#     over a backslash.
#   * the `<=` branch -- a short line is the title, untouched.
#   * the last branch -- 59 characters plus an ellipsis, so the result is still 60
#     and still reads as truncated rather than as an error, and trimmed so it never
#     ends in the whitespace the cut landed on.
#
# "Matching" and not "identical", and the one deliberate difference is worth
# naming: on a transcript with CRLF line endings, `memoLabel.js` yields a label
# with a carriage return still on it, because it trims the transcript before
# splitting and never trims the line. This strips it. A stray control character
# rendered once is untidy; the same character *persisted into a column* is a thing
# every later reader of `title` inherits, so the two agree on what the label says
# and this side declines to store the wart.
#
# The empty case is NULL rather than '', because a blank title renders as an
# untitled row with extra steps.
#
# **Postgres's btrim and rtrim default to space only, not to whitespace.** That is
# the trap in this expression and it was caught by running it rather than by
# reading it. The first version trimmed with the defaults and split before
# trimming, which produced three wrong titles on a real Postgres:
#
#   * a text memo pasted with CRLF line endings -- `Sunday Meeting\r\nWe...` --
#     titled `Sunday Meeting` **with a carriage return still on the end**, because
#     splitting on chr(10) leaves the \r and a bare btrim does not take it.
#   * a transcript with a leading newline titled **NULL**, because splitting first
#     makes the empty leading line the "first line". That is the one rule this
#     column exists to uphold, broken by the code meant to uphold it. The API trims
#     a typed memo (`StoreMemoRequest`) so it is not reachable from there today,
#     which is exactly why it would have survived to be reachable later.
#   * a leading tab kept verbatim in the title.
#
# Hence `_TITLE_TRIM` below, an explicit character set, and hence the outer trim on
# the *transcript* before the split rather than only on the line after it.
_TITLE_TRIM = r"E' \t\r\n'"

# The first line of the transcript, trimmed at both ends. One sub-expression rather
# than three copies, since the CASE has to test it, return it and truncate it.
_FIRST_LINE = (
    f"btrim(split_part(btrim(transcript, {_TITLE_TRIM}), chr(10), 1), {_TITLE_TRIM})"
)

_FALLBACK_TITLE = f"""
            CASE
                WHEN {_FIRST_LINE} = '' THEN NULL
                WHEN length({_FIRST_LINE}) <= %(title_chars)s THEN {_FIRST_LINE}
                ELSE rtrim(left({_FIRST_LINE}, %(title_chars)s - 1), {_TITLE_TRIM}) || '…'
            END"""

# Written to `last_error` when a claim expires. Reaches the browser, so it is a
# sentence rather than a status code, and it says what happens next -- a requeued
# memo is not in trouble and the UI (MEMO-17) should not present it as if it were.
REAPED_MESSAGE = (
    "This memo was interrupted while it was being processed. It has been queued to try again."
)
ABANDONED_MESSAGE = (
    "This memo could not be processed after several attempts. It was interrupted each time "
    "rather than failing with a reason."
)
ABANDONED_ENRICHMENT_MESSAGE = (
    "The transcript is complete, but the memo was interrupted before a title and summary "
    "could be generated."
)


# The ceiling on one backoff, however many attempts have been made.
#
# It never binds on the shipped configuration -- three attempts at a 30s base reach
# 60s -- and it is here for the configuration somebody else chooses. `MAX_ATTEMPTS`
# is a knob, and doubling is faster than it looks: at 10 attempts the last wait is
# 4.3 hours, which is not resilience, it is a memo nobody is coming back for.
#
# Past around forty attempts it stops being a usability problem. The first version
# of this comment said the interval would overflow and the statement would raise;
# checked against Postgres 16 instead, and it does something quieter and worse --
# `make_interval` **saturates** at 2562047788:00:54.775807, about 292,471 years, and
# returns it without complaint. A memo given that `next_attempt_at` is `queued`
# rather than `processing`, so the reaper's predicate never sees it either. It is
# simply gone, with every status column reading normal. An error would at least
# have been an error.
#
# An hour, because it is the same order as the claim lease: waiting longer than a
# whole abandoned job takes to be noticed is not a delay anyone chose.
MAX_BACKOFF_SECONDS = 3600.0


@dataclass(frozen=True)
class RetryPolicy:
    """
    The three numbers that decide when a job is retried, given up on, or reaped.

    Grouped rather than passed one at a time because they constrain each other and
    a caller that sets one has to see the others. ``reap_after_seconds`` must
    exceed the longest a healthy job can run (memo_ai/pipeline.py computes that
    budget, and the worker checks it at boot); ``max_attempts`` bounds both the
    retry path and the reaper, so the two cannot disagree about when a memo is
    finished with.
    """

    max_attempts: int
    backoff_seconds: float
    reap_after_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            max_attempts=settings.max_attempts,
            backoff_seconds=settings.retry_backoff_seconds,
            reap_after_seconds=settings.reap_after_seconds,
        )

    def delay_for(self, attempts: int) -> float:
        """
        How long to wait before attempt ``attempts + 1``: exponential, with jitter.

        The jitter is not decoration. Two replicas that fail the same way at the
        same moment -- a model still downloading, a database that just came back --
        would otherwise retry in lockstep forever, so every attempt after the first
        arrives as a burst of the same size as the one that just failed. A uniform
        +/-20% spreads them without changing the shape of the curve.

        Exponent from ``attempts`` rather than from a counter this class keeps,
        because the count lives on the row and is incremented by the claim. That is
        what makes the backoff survive a process that never runs this code -- a
        memo whose worker was killed comes back with a larger ``attempts`` and gets
        the longer delay it is owed, decided by whichever replica reaps it.

        Capped at :data:`MAX_BACKOFF_SECONDS`, and the jitter is applied after the
        cap so that a capped delay is still spread rather than becoming the one
        instant every waiting memo shares.
        """
        doubled = self.backoff_seconds * (2 ** max(0, attempts - 1))

        return min(doubled, MAX_BACKOFF_SECONDS) * random.uniform(0.8, 1.2)


@dataclass(frozen=True)
class Reaped:
    """What one pass of the reaper did, as three lists of ids, for the log."""

    requeued: list[UUID]
    failed: list[UUID]
    salvaged: list[UUID]

    def __bool__(self) -> bool:
        return bool(self.requeued or self.failed or self.salvaged)


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
    # `datetime` are both microsecond-precision, so the round trip is exact, and that
    # was checked from both directions rather than inferred: a value that had been
    # out to Python and back still matches in the WHERE clause, and two claims of the
    # same row 3.6 ms apart produced tokens that differ.
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
# write. That is what makes the count survive a `SIGKILL` mid-work, and it is the
# whole reason a poison memo terminates at `MAX_ATTEMPTS` instead of retrying
# forever: a memo that destroys its worker never reaches a failure handler, so a
# counter incremented on the way *out* of a job would never move for exactly the
# memo that needs bounding. Here it moves before any of our code runs.
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

# --------------------------------------------------------------------------
# The two commit points
# --------------------------------------------------------------------------
#
# One job, two writes, and the row stays `processing` between them. That is the
# whole of MEMO-16's central mechanism, and what it buys is that transcription and
# enrichment fail independently without a second status column to say which stage
# is owed. `transcript IS NULL` already answers that question -- see `owed_audio`
# in memo_ai/pipeline.py -- so a crash in the gap loses the enrichment and keeps
# the transcript, and a re-claim skips straight to the second write.
#
# On a hosted provider that property is money: the paid call is committed before
# anything that can fail cheaply runs after it. Played out against a real Postgres
# rather than left as an argument: a memo whose transcript was committed and whose
# claim was then reaped came back from the next claim with `transcript` populated
# and `cost_micro_usd` intact, so the second attempt skipped transcription and
# published the same text.

# Commit 1. Everything transcription produced, and no status change.
#
# Leaving `status` alone is the point of the statement rather than an omission --
# writing 'ready' here would publish a memo with no title, and writing anything
# else would need a status the schema does not have. `processing` already means
# "claimed and not finished", which is exactly true between these two writes.
#
# COALESCE on all five, which is not defensiveness: `duration_ms` and
# `cost_micro_usd` are frequently absent on a result that still has a transcript,
# and a bare assignment would erase what an earlier attempt measured. The useful
# side effect is that this statement *cannot* null a transcript out. MEMO-16's
# goal is that a transcript is never lost, and this is the shape that makes losing
# one require editing the SQL rather than passing the wrong argument.
#
# `last_error` is cleared here, and only here on this path. A memo that failed
# twice and transcribed on the third attempt still carries the second attempt's
# sentence, and `last_error` reaches the browser -- a ready memo displaying the
# error it recovered from is a worse bug than no message at all. Cleared at *this*
# commit rather than the next so it is also gone for a job that crashes in the gap.
_COMMIT_TRANSCRIPT = """
    UPDATE memos
       SET transcript = COALESCE(%(transcript)s, transcript),
           stt_provider = COALESCE(%(stt_provider)s, stt_provider),
           stt_model = COALESCE(%(stt_model)s, stt_model),
           duration_ms = COALESCE(%(duration_ms)s, duration_ms),
           cost_micro_usd = COALESCE(%(cost_micro_usd)s, cost_micro_usd),
           last_error = NULL,
           last_error_code = NULL
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""

# Commit 2. `status='ready'`, whatever enrichment produced, and a title either way.
#
# Reached on **both** enrichment outcomes, which is the rule this statement exists
# to enforce: enrichment is best-effort and may not fail a memo. A failure arrives
# here as a non-NULL `enrichment_error` beside NULL enrichment, and the row still
# goes to 'ready' carrying its transcript. db/migrations/001_init.sql separates
# `enrichment_error` from `last_error` for this reason and says so at the column.
#
# The title is a COALESCE over four sources, and the order is the argument:
# whatever the enricher produced, then whatever is already on the row, then the
# heuristic `memo_ai/titles.py` cuts out of the transcript, then `_FALLBACK_TITLE`.
# Keeping an existing title ahead of both fallbacks is what stops a re-run from
# downgrading a real title -- and it is also what makes the column safe for a person
# to edit, which PATCH /api/memos/{id} now lets them do.
#
# **Two fallbacks rather than one, and the SQL one is still last for the reason it
# was written.** `_FALLBACK_TITLE` is the first sixty characters of the transcript;
# `titles.title_for` is a short phrase cut out of it -- "Meeting with my friend John"
# where the other gives "Tomorrow I will have a meeting with my friend John at 15a…".
# The heuristic is the better answer wherever it can be had, so it goes first. But it
# runs in Python, and Python is not always holding the text: the reaper's salvage
# branch updates rows in bulk with no job in memory at all. So the SQL expression
# stays as the last resort rather than being replaced, and `_REAP_SALVAGE` still uses
# it alone.
#
# `finish_ready` is handed the text explicitly for the same reason. A job resumed
# after commit 1 produced no transcript *this time* -- it is on the row and nowhere
# in memory -- so the pipeline passes whichever of the two it has, which is the
# expression it already computes for the enricher.
#
# `enrichment_error` is assigned outright rather than COALESCEd, unlike everything
# around it. It is the one column here whose *absence* is information: an
# enrichment that succeeded has to clear the previous attempt's complaint, and
# COALESCE would leave a ready, titled, summarised memo still claiming enrichment
# had failed.
#
# `enriched_at` is a CASE on a boolean the caller passes rather than on the
# arguments, because "did an enricher run and produce something" is not derivable
# from them: `NoEnrichment` returns nothing at all and an enricher can legally
# return a title alone. memo_ai/enrich.py's `is_empty` is what answers it.
_FINISH_READY = f"""
    UPDATE memos
       SET status = 'ready',
           title = COALESCE(
                       %(title)s,
                       title,
                       %(heuristic_title)s,
                       {_FALLBACK_TITLE}
                   ),
           summary = COALESCE(%(summary)s, summary),
           tags = COALESCE(%(tags)s::text[], tags),
           category = COALESCE(%(category)s, category),
           enrichment_error = %(enrichment_error)s,
           enriched_at = CASE WHEN %(enriched)s THEN now() ELSE enriched_at END,
           last_error = NULL,

           -- Cleared with the sentence, never without it. Every write in this file sets
           -- both error columns or clears both: a code with no sentence explains nothing
           -- to a person, and a sentence with no code can be classified by nothing.
           -- test_failures.py checks the statements for it, because both places this rule
           -- was broken were statements written before the code column existed, where one
           -- cleared line looks like the whole job.
           last_error_code = NULL
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""

# --------------------------------------------------------------------------
# Giving up, and trying again
# --------------------------------------------------------------------------

# The terminal failure write.
#
# `failed` means **no transcript**, which is the invariant the rest of this file is
# arranged around: it is reachable only from a transcription failure, and only once
# `attempts` has reached the cap. Enrichment cannot produce it, and neither can the
# reaper on a row that has text -- see `_REAP_SALVAGE`.
#
# `next_attempt_at` is deliberately not touched. A failed row is not due for
# anything, and the claim predicate already excludes it by status; moving the
# timestamp would only make a terminal row look scheduled in the one place a person
# goes to ask why nothing is happening.
#
# `duration_ms` is COALESCEd rather than assigned. A memo refused for being too
# long is refused *by* a duration and the row wants it beside the sentence; most
# other failures never get far enough to measure anything, and a bare assignment
# would then erase what an earlier attempt recorded.
_FAIL = """
    UPDATE memos
       SET status = 'failed',
           locked_at = NULL,
           last_error = %(last_error)s,
           last_error_code = %(last_error_code)s,
           duration_ms = COALESCE(%(duration_ms)s, duration_ms)
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""

# The retry write: back to `queued`, due after a backoff, with the reason on the row.
#
# `locked_at = NULL` is the line that matters, and it is why this statement is not
# simply `_FAIL` with a different status. `locked_at` is the fence token; a row
# handed back to the queue still carrying the old one could be written by the
# previous claim after a *new* worker had taken it. Releasing it makes every fenced
# write from the old attempt match zero rows, which is the correct outcome and the
# one `_fenced` logs.
#
# `last_error` is written on a *non*-terminal state on purpose. The memo is going
# to be retried and the row says so through `status='queued'`, but a person
# watching a memo take three minutes deserves to know why -- and MEMO-17's failure
# UI is the reader. It is cleared by the next commit that succeeds.
#
# `attempts` is not incremented here. The claim owns that counter (see `_CLAIM`),
# which is what makes it survive a worker that never reaches this statement at all.
_RETRY = """
    UPDATE memos
       SET status = 'queued',
           locked_at = NULL,
           last_error = %(last_error)s,
           last_error_code = %(last_error_code)s,
           next_attempt_at = now() + make_interval(secs => %(delay_seconds)s),
           duration_ms = COALESCE(%(duration_ms)s, duration_ms)
     WHERE id = %(id)s
       AND locked_at = %(locked_at)s
"""

# --------------------------------------------------------------------------
# The reaper
# --------------------------------------------------------------------------
#
# Three statements over the same predicate -- `processing`, past its lease -- split
# by what the row has rather than merged into one UPDATE full of CASE expressions.
# Each WHERE reads as the sentence describing the case it handles, and each SET
# does one thing, which is worth more here than one round trip fewer.
#
# The lease has to exceed the longest a healthy job can run or this reaps work in
# progress. That number is derived rather than guessed: memo_ai/pipeline.py's
# `job_budget_seconds` sums the ffmpeg, ffprobe, model-load and decode deadlines,
# and the worker compares it against the configured lease at boot.
#
# None of these three needs `FOR UPDATE SKIP LOCKED`, unlike the claim, and the
# reason is worth stating because the claim's comment argues the opposite for
# itself. Both replicas run these concurrently; under READ COMMITTED the second one
# blocks on a row the first is updating and then **re-evaluates its WHERE against
# the updated row**, which no longer says `processing`. So it matches nothing and
# moves on. The claim needs SKIP LOCKED because it must not block at all -- it runs
# twice a second per replica and waiting behind a peer would serialise the queue --
# while the reaper runs once a minute and correctness, not latency, is its problem.
# Checked on two connections against one expired claim: the first pass requeued it,
# the second returned nothing rather than requeueing it a second time.
#
# The whole of the below was verified against a real Postgres, because a reaper that
# never fires and a reaper that fires correctly look identical from the outside. A
# claim aged past the lease was requeued with its lock released and a fresh
# `next_attempt_at`; the same claim before the lease expired was left alone; and a
# worker whose claim had been reaped could not then write to the row -- its
# transcript commit, its publish and its failure write all matched zero rows while
# the new claimant's landed.
#
# One asymmetry across the three, because it looks like an oversight and is not:
# the requeue **assigns** `last_error` while the two terminal statements COALESCE
# it. What can be on that column at reap time is an error from an *earlier*
# attempt, since the attempt being reaped was killed and wrote nothing. For a memo
# going back to the queue, "interrupted, trying again" is the true and more useful
# description of where it now is, and the older sentence is about an attempt that
# is over. For a memo being given up on, the older sentence is the only real reason
# anyone recorded, and overwriting it with ABANDONED_MESSAGE would replace a
# diagnosis with a shrug -- worse, with a *false* one, since that message says the
# memo failed without a reason.

# Still has attempts left: hand it back to the queue after a backoff.
#
# The backoff is computed in SQL here, unlike on the retry path, because this
# statement resolves many rows at once and they have not all burned the same number
# of claims. `RetryPolicy.delay_for` would produce one delay for the batch and
# release a memo on its third attempt as eagerly as one on its first. The
# expression is that method, transcribed: base, doubled per attempt already made,
# capped, jittered +/-20%. `random()` inside the UPDATE is evaluated per row, so two
# memos reaped together do not come back in the same instant.
#
# The `least(...)` is the cap, and this is the side where leaving it out is
# unrecoverable rather than merely slow: `make_interval` saturates instead of
# raising, so a large `MAX_ATTEMPTS` writes a `next_attempt_at` roughly 292,471
# years out and the memo is never claimed and never reaped again. See
# MAX_BACKOFF_SECONDS.
_REAP_REQUEUE = """
    UPDATE memos
       SET status = 'queued',
           locked_at = NULL,
           last_error = %(last_error)s,
           last_error_code = %(last_error_code)s,
           next_attempt_at = now() + make_interval(
               secs => least(
                           %(backoff_seconds)s * power(2, greatest(attempts - 1, 0)),
                           %(max_backoff_seconds)s
                       )
                       * (0.8 + random() * 0.4)
           )
     WHERE status = 'processing'
       AND locked_at < now() - make_interval(secs => %(lease_seconds)s)
       AND attempts < %(max_attempts)s
    RETURNING id
"""

# Out of attempts and never produced a transcript: this is the poison memo, and
# `failed` is the honest end for it.
#
# The cap is checked here rather than in the claim predicate because a row the
# claim silently skipped would sit in `processing` forever with nothing to explain
# it. MEMO-08 set that rule -- a visible dead end beats an invisible one -- and it
# holds more strongly now that the invisible version would also never be reaped.
_REAP_FAIL = """
    UPDATE memos
       SET status = 'failed',
           locked_at = NULL,
           last_error = COALESCE(last_error, %(last_error)s),
           last_error_code = COALESCE(last_error_code, %(last_error_code)s)
     WHERE status = 'processing'
       AND locked_at < now() - make_interval(secs => %(lease_seconds)s)
       AND attempts >= %(max_attempts)s
       AND transcript IS NULL
    RETURNING id
"""

# Out of attempts but the transcript is already committed: publish it.
#
# The case exists because the two commit points are two commits: a memo can be
# killed in the gap between them, three times over, and end up with its text safely
# on the row and no worker left willing to claim it. Sending that to `failed` would
# break the rule that `failed` means no transcript, and would hide a completed
# transcription behind an error badge.
#
# So it goes to `ready` with the same fallback title `_FINISH_READY` would have
# given it, and `enrichment_error` explains the missing summary. `last_error` is
# cleared: transcription did not fail here, enrichment never got to run, and
# leaving a stale interruption notice on a ready memo would say otherwise.
#
# `last_error_code` is cleared **with** it, and the pairing is the rule rather than a
# detail of this statement: every write in this file either sets both columns or
# clears both, because a row carrying a code with no sentence can explain nothing to a
# person, and one carrying a sentence with no code can be classified by nothing. This
# is the statement where the rule is easiest to break, since it was written before the
# code column existed and clearing one line looks complete.
#
# Both of these branches were run on a real Postgres with two rows at the cap in the
# same pass -- one with a transcript, one without -- and the pass resolved each to
# its own outcome and requeued neither.
_REAP_SALVAGE = f"""
    UPDATE memos
       SET status = 'ready',
           locked_at = NULL,
           last_error = NULL,
           last_error_code = NULL,
           title = COALESCE(title, {_FALLBACK_TITLE}),
           enrichment_error = COALESCE(enrichment_error, %(enrichment_error)s)
     WHERE status = 'processing'
       AND locked_at < now() - make_interval(secs => %(lease_seconds)s)
       AND attempts >= %(max_attempts)s
       AND transcript IS NOT NULL
    RETURNING id
"""


class MemoQueue:
    """
    The statements above, over one connection.

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

    def __init__(self, connection: psycopg.Connection, policy: RetryPolicy) -> None:
        self._connection = connection
        self._policy = policy

    def claim(self) -> ClaimedMemo | None:
        """
        Take the oldest due memo, or ``None`` when there is nothing to do.

        ``None`` is the ordinary case, not an error: this runs on every poll and
        most polls find an empty queue.
        """
        with self._connection.cursor(row_factory=class_row(ClaimedMemo)) as cursor:
            cursor.execute(_CLAIM)

            return cursor.fetchone()

    def commit_transcript(
        self,
        memo: ClaimedMemo,
        transcript: Transcript,
        duration_ms: int | None = None,
    ) -> bool:
        """
        Commit point 1: the transcript is safe, the row stays ``processing``.

        False if the fence lost, and the caller must stop rather than continue to
        the second commit -- the row belongs to whoever holds the claim now, and
        everything after this point would be written against their attempt.
        """
        return self._fenced(
            _COMMIT_TRANSCRIPT,
            {
                "id": memo.id,
                "locked_at": memo.locked_at,
                "transcript": transcript.text,
                "stt_provider": transcript.provider,
                "stt_model": transcript.model,
                # None for a text memo, which has no audio and so no length. That
                # is the same NULL the row was inserted with, and COALESCE keeps it.
                "duration_ms": duration_ms,
                "cost_micro_usd": transcript.cost_micro_usd,
            },
            memo,
            "transcript",
        )

    def finish_ready(
        self,
        memo: ClaimedMemo,
        enrichment: Enrichment | None = None,
        enrichment_error: str | None = None,
        text: str | None = None,
    ) -> bool:
        """
        Commit point 2: publish the memo, enriched or not. False if the fence lost.

        Both enrichment arguments absent is the shipped configuration rather than a
        degenerate case -- no enricher is wired up until MEMO-21 -- and it writes a
        memo that is ``ready`` with a generated title and nothing else claimed
        about it.

        ``text`` is the transcript this memo now has, which the caller passes rather
        than this method reading it off ``memo``: at commit point 2 a fresh voice
        memo's transcript is on the row and *not* on the claim, because the claim
        happened before it existed. The pipeline already computes the right one for
        the enricher, so it hands over the same value. Omitted, the title falls
        through to the SQL expression, which is what the reaper relies on.
        """
        enriched = enrichment is not None and not enrichment.is_empty()
        complaint = None if enrichment_error is None else _truncate(enrichment_error)

        return self._fenced(
            _FINISH_READY,
            {
                "id": memo.id,
                "locked_at": memo.locked_at,
                "title": None if enrichment is None else enrichment.title,

                # The heuristic, ahead of the SQL fallback and behind everything
                # else. None when this process has no text, which is when the SQL
                # expression earns its place -- see the statement's own note.
                "heuristic_title": titles.title_for(text),

                "summary": None if enrichment is None else enrichment.summary,
                # A list, not the frozen tuple: psycopg maps a Python list to
                # `text[]`, and an empty one would be an empty array rather than
                # the NULL that COALESCE reads as "leave the column alone".
                "tags": list(enrichment.tags) if enrichment and enrichment.tags else None,
                "category": None if enrichment is None else enrichment.category,
                "enrichment_error": complaint,
                "enriched": enriched,
                "title_chars": FALLBACK_TITLE_CHARS,
            },
            memo,
            "finish",
        )

    def fail_or_retry(
        self,
        memo: ClaimedMemo,
        error: str,
        *,
        code: str,
        retryable: bool,
        duration_ms: int | None = None,
    ) -> bool:
        """
        End a failed attempt: back to the queue if anything is left, else ``failed``.

        The caller supplies ``retryable`` because only it knows what was raised --
        an audio file ffmpeg cannot decode will not decode on the third attempt
        either, and spending two more claims and 90 seconds to confirm that is
        worse than saying so now. The attempt count is this class's half of the
        decision, because it lives on the row.

        ``code`` travels with the sentence for the same reason and from the same
        place: only the raise site knows which *kind* of failure this is, and the
        sentence cannot be parsed back into one. It is required rather than
        defaulted, so a new failure path has to say what it is instead of silently
        writing NULL into a column the frontend branches on. memo_ai/failures.py has
        the vocabulary and the argument.

        Returns whether the write landed, not what it decided; the decision is
        logged here, where both numbers are in hand.
        """
        exhausted = memo.attempts >= self._policy.max_attempts

        if exhausted or not retryable:
            log.info(
                "memo %s: failing after attempt %d of %d (%s, %s)",
                memo.id,
                memo.attempts,
                self._policy.max_attempts,
                code,
                "attempts exhausted" if exhausted else "not retryable",
            )

            return self._fenced(
                _FAIL,
                {
                    "id": memo.id,
                    "locked_at": memo.locked_at,
                    "last_error": _truncate(error),
                    "last_error_code": code,
                    "duration_ms": duration_ms,
                },
                memo,
                "fail",
            )

        delay = self._policy.delay_for(memo.attempts)

        log.info(
            "memo %s: attempt %d of %d failed (%s), retrying in %.1fs",
            memo.id,
            memo.attempts,
            self._policy.max_attempts,
            code,
            delay,
        )

        return self._fenced(
            _RETRY,
            {
                "id": memo.id,
                "locked_at": memo.locked_at,
                "last_error": _truncate(error),
                "last_error_code": code,
                "delay_seconds": delay,
                "duration_ms": duration_ms,
            },
            memo,
            "retry",
        )

    def reap(self) -> Reaped:
        """
        Take back every claim that has outlived the lease, and resolve the dead ones.

        Three statements, unfenced, because this is the one operation that acts on
        rows it does not hold -- the whole point is to override a claim whose owner
        is gone. What keeps it from stealing live work is the lease in the WHERE,
        not a token.

        Deliberately not run inside a transaction spanning all three. The three
        predicates are disjoint (attempts below the cap; at the cap with no
        transcript; at the cap with one), so no row can be matched by two of them,
        and a connection lost between statements leaves the remaining rows in
        ``processing`` for the next pass rather than half-resolved.
        """
        return Reaped(
            requeued=self._reap(
                _REAP_REQUEUE,
                {
                    "last_error": REAPED_MESSAGE,
                    "last_error_code": failures.INTERRUPTED,
                    "backoff_seconds": self._policy.backoff_seconds,
                    "max_backoff_seconds": MAX_BACKOFF_SECONDS,
                    "lease_seconds": self._policy.reap_after_seconds,
                    "max_attempts": self._policy.max_attempts,
                },
            ),
            failed=self._reap(
                _REAP_FAIL,
                {
                    "last_error": ABANDONED_MESSAGE,
                    # COALESCEd in the statement, like the sentence beside it and for
                    # the same reason: if an earlier attempt recorded a real diagnosis,
                    # that is the one worth keeping. A memo abandoned after a
                    # `no_speech` attempt therefore stays discardable, which is right --
                    # it is still a recording with nothing in it.
                    "last_error_code": failures.ABANDONED,
                    "lease_seconds": self._policy.reap_after_seconds,
                    "max_attempts": self._policy.max_attempts,
                },
            ),
            salvaged=self._reap(
                _REAP_SALVAGE,
                {
                    "enrichment_error": ABANDONED_ENRICHMENT_MESSAGE,
                    "lease_seconds": self._policy.reap_after_seconds,
                    "max_attempts": self._policy.max_attempts,
                    "title_chars": FALLBACK_TITLE_CHARS,
                },
            ),
        )

    def _reap(self, sql: str, params: dict[str, object]) -> list[UUID]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, params)

            # The ids rather than the count, so the log names the memos a person can
            # then go and look at. Reaping is rare and the lists are short.
            return [row[0] for row in cursor.fetchall()]

    def _fenced(self, sql: str, params: dict[str, object], memo: ClaimedMemo, what: str) -> bool:
        """
        Run a write fenced on ``locked_at`` and report whether it landed.

        Checking ``rowcount`` is the point of this method. The fence makes the
        statement a legal no-op whenever this worker is no longer the owner of the
        claim, and an UPDATE that matched nothing is a success as far as the driver
        is concerned -- so without this check a worker that lost the row would log a
        completed job while the row said something else entirely.

        That stopped being hypothetical when the reaper landed: a job reaped as
        stuck is re-claimed with a *new* ``locked_at``, and the original -- still
        running, because a reaped job is not a stopped one -- must not be able to
        overwrite the new attempt. Fencing is also why the two commit points never
        touch ``locked_at``: the token has to stay put across both of them, so a
        job that has committed its transcript can still commit its enrichment. The
        three writes that *do* clear it -- ``_FAIL``, ``_RETRY`` and the reaper's --
        are exactly the ones that end the claim.

        Played out against a real Postgres rather than left as an argument, because
        a fence that never loses and a fence that never fires look identical from
        the outside. Worker A claims; a reaper requeues the row; worker B re-claims
        it and gets a different token; A then writes. A's ``finish_ready`` returned
        False, the transcript column was still NULL, and the row still carried B's
        claim -- and B's own write then landed normally. ``fail()`` is fenced the
        same way and was checked the same way: A could not move the row to
        ``failed`` either.

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

"""
What one job does, between the claim and the result write.

Everything slow lives here, and nothing here holds a transaction open -- that
separation is the design, not an implementation detail. memo_ai/db.py has the
experiment behind it.

Four steps: normalize, measure, transcribe, enrich. The order is the point. The
duration comes off the *normalized* file, and it is checked against
``MAX_AUDIO_SECONDS`` before a provider is called, so a memo that is too long costs
one ffmpeg run and nothing else -- no hosted request, no model load.
memo_ai/audio.py has the reason the duration cannot be read off the original
instead.

**The job commits twice, and the row is ``processing`` in between.** That is
MEMO-16's shape and it is what makes the two halves fail independently:

  1. transcription succeeds -> the transcript, its provider, model, duration and
     cost are committed. Nothing about the status changes.
  2. enrichment finishes, however it finished -> the memo becomes ``ready``, with
     a title either way.

The asymmetry is the design. A transcript is the memo and must never be lost; a
title and a summary are conveniences and must never cost one. So ``failed`` is
reachable only from step 1, and only after the attempts are used up -- an
enrichment that raises lands in ``enrichment_error`` on a ``ready`` row.

The property that pays for the split is on the *re-claim*. ``owed_audio`` below
tests ``transcript IS NULL`` to decide whether transcription is owed, so a job that
died in the gap between the two commits resumes at step 2 and never calls the
provider again. On a hosted provider that is the difference between one bill and
two for the same memo.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from memo_ai import audio, failures, rss
from memo_ai.enrich import NO_ENRICHMENT, Enricher, Enrichment, EnrichmentError
from memo_ai.memos import ClaimedMemo, MemoQueue
from memo_ai.stt import local
from memo_ai.stt.base import SttError, SttProvider, SttUnavailable

log = logging.getLogger(__name__)

# What goes in `last_error` when the exception was not one we classified. The real
# detail goes to the log instead.
#
# The reason is that `last_error` is part of the API's response projection, so it
# reaches the browser -- and an arbitrary exception's text is not something this
# code chose. A psycopg connection error, for one, stringifies as `connection to
# server at "db" (172.18.0.2), port 5432 failed: ...`, which puts the internal
# topology in an HTTP response to answer a question the user did not ask. Only
# messages an implementation wrote for this column go in it; see SttError, and
# memo_ai/audio.py for the same rule applied to ffmpeg's stderr.
UNEXPECTED_ERROR = "Unexpected worker error. See the ai-worker logs for details."

# The same rule for the second half of the job. Kept separate from the sentence
# above because the two land in different columns and mean different things to a
# reader: this one appears on a memo that is `ready` and has its transcript.
UNEXPECTED_ENRICHMENT_ERROR = (
    "The transcript is complete, but generating a title and summary failed "
    "unexpectedly. See the ai-worker logs for details."
)


def run_job(
    queue: MemoQueue,
    memo: ClaimedMemo,
    provider: SttProvider,
    audio_dir: Path,
    max_audio_seconds: float,
    enricher: Enricher = NO_ENRICHMENT,
) -> None:
    """
    Do the work the claim promised, committing the transcript before enriching it.

    The ``try`` covers the audio work and the transcription and nothing else,
    which is deliberate. Every write below is outside it, so a database failure
    while recording a result propagates to the loop, which reconnects -- and
    leaves the row in ``processing`` for the reaper, because a result that could
    not be written is exactly the case the reaper exists for. Swallowing it here
    would instead mark the memo done on the strength of a write that did not
    happen.

    Enrichment has its own containment (:func:`_enriched`) rather than sharing
    this one, and that is the difference between the two stages expressed in
    control flow: a failure up here returns without publishing the memo, and a
    failure down there is a value that the publishing write carries.
    """
    started = time.monotonic()

    # Assigned the moment ffprobe answers, and read by every write below.
    # Persisting it on the failure paths too is what keeps a memo that failed
    # *after* being measured from showing a blank length in the UI.
    duration_ms: int | None = None

    try:
        with owed_audio(memo, provider, audio_dir, max_audio_seconds) as prepared:
            # None means this memo owes no transcript -- see owed_audio.
            duration_ms = None if prepared is None else prepared.duration_ms
            transcript = (
                None
                if prepared is None
                else provider.transcribe(prepared.path, memo.language)
            )
    except audio.AudioTooLong as error:
        # Before AudioError and SttError, which it subclasses. It is the one
        # failure that carries its own duration, because the duration is what
        # caused it, and the generic handler below would drop that.
        #
        # Not retryable, and this is the clearest case of it: two more attempts
        # would re-measure the same file and reach the same refusal, three minutes
        # later, having told the user nothing new.
        log.info("memo %s: refused for length: %s", memo.id, error)
        queue.fail_or_retry(
            memo, str(error), code=error.code, retryable=False, duration_ms=error.duration_ms
        )

        return
    except SttUnavailable as error:
        # Retryable, and the reason this subclass exists at all: it means "this
        # provider cannot run *here, now*" -- a model still downloading, a load
        # that ran out of memory under two replicas -- rather than "this audio has
        # no words in it". Both of those resolve on their own, and the backoff is
        # sized so three attempts span long enough for a 1.6 GB fetch to land.
        log.warning("memo %s: provider unavailable: %s", memo.id, error)
        queue.fail_or_retry(
            memo, str(error), code=error.code, retryable=True, duration_ms=duration_ms
        )

        return
    except SttError as error:
        # Classified, so the message is safe and useful on the row -- and terminal.
        # Every provider in the chain is fed the same normalized file, so a file
        # that produced no transcript once will produce none again; memo_ai/stt/
        # chain.py declines to walk the fallback for the same reason and
        # AudioError's docstring states it. This is also the corrupt-file case in
        # MEMO-16's acceptance: one attempt, a readable sentence, no hanging.
        log.warning("memo %s: no transcript: %s", memo.id, error)
        queue.fail_or_retry(
            memo, str(error), code=error.code, retryable=False, duration_ms=duration_ms
        )

        return
    except Exception:
        # Retryable, unlike the classified failures above, and the asymmetry is on
        # purpose: an exception nobody classified is one nobody has shown to be
        # deterministic, and the cheap assumption is that it might not be. Three
        # attempts cost a poison memo two minutes of queue time and buy a genuinely
        # transient fault its recovery.
        #
        # log.exception, so the traceback is in the container logs even though the
        # row only carries the generic sentence above.
        log.exception("memo %s: unexpected error while transcribing", memo.id)
        queue.fail_or_retry(
            memo,
            UNEXPECTED_ERROR,
            # The one code not taken from an exception, because the whole point of this
            # branch is that nothing classified this one. See memo_ai/failures.py.
            code=failures.UNEXPECTED,
            retryable=True,
            duration_ms=duration_ms,
        )

        return

    # Commit 1. Skipped when nothing was transcribed -- a text memo, or a job
    # resuming after an earlier attempt already committed one.
    if transcript is not None and not queue.commit_transcript(memo, transcript, duration_ms):
        # The fence lost, so this worker no longer owns the row. Returning rather
        # than carrying on is the whole point of checking: enriching and publishing
        # against someone else's claim is exactly what fencing prevents.
        return

    # Whichever text is now on the row: the one just produced, or the one an
    # earlier attempt committed. The enricher is owed the memo's transcript, not
    # this job's output, and for a resumed job those differ.
    text = memo.transcript if transcript is None else transcript.text
    enrichment, enrichment_error = _enriched(enricher, memo, text)

    # Commit 2. Runs on both enrichment outcomes -- that is the rule.
    #
    # `text` goes to the write as well as to the enricher, and for the same reason it
    # was computed that way: it is whichever transcript the row now holds. The write
    # cuts a fallback title out of it when the enricher produced none -- which since
    # MEMO-21 is the enrichment-failed path and `ENRICH_PROVIDER=none`, rather than
    # every memo. Passing it rather than letting the statement read
    # `memo.transcript` is what makes that work for a *fresh* voice memo, whose
    # claim predates its own transcript.
    if queue.finish_ready(memo, enrichment, enrichment_error, text=text):
        log.info(
            # `rss=` is MEMO-22's, and this line is where it goes rather than in a
            # periodic tick of its own. Resident memory is a property of the
            # process and not of the memo, so what makes it worth reading is
            # *when* it is sampled: both models load lazily, so the interesting
            # transitions -- 18 MB to 1.65 GB on the first transcription, and
            # again on the first enrichment -- happen exactly at the boundary this
            # line already marks. A separate timer would sample them at random and
            # add a log line a minute per replica saying nothing changed.
            #
            # **`brief` and not `describe`**, which is the difference between free
            # and 10.8 ms of page-table walking per memo on a loaded worker -- and
            # paid whether or not this line is emitted, since a logging argument is
            # evaluated either way. The shared/private split does not change from
            # memo to memo; the worker states it once at boot, where the process is
            # small enough for the walk to cost nothing. memo_ai/rss.py has the
            # measurement.
            #
            # `docker compose logs ai-worker | grep rss` is the whole of the RAM
            # half of that task, and memo_ai/costs.py's footer says so.
            "memo %s ready in %.0fms (attempt %d, %s%s%s, rss=%s)",
            memo.id,
            (time.monotonic() - started) * 1000,
            memo.attempts,
            "transcribed" if transcript else "transcript already present",
            "" if duration_ms is None else f", {duration_ms}ms of audio",
            "" if enrichment_error is None else ", enrichment failed",
            rss.brief(),
        )


def _enriched(
    enricher: Enricher,
    memo: ClaimedMemo,
    transcript: str | None,
) -> tuple[Enrichment | None, str | None]:
    """
    Run the enricher and turn whatever it did into two values the row can hold.

    Never raises. That is the contract this function exists to provide, and it is
    why the caller can put its result straight into the publishing write: there is
    no enrichment outcome that stops a transcribed memo reaching ``ready``.

    The classification mirrors the transcription half exactly -- a classified
    ``EnrichmentError`` puts its own sentence on the row, and anything else gets a
    written one while the traceback goes to the log. ``enrichment_error`` is a
    column the API can project, so the same rule applies: only messages this
    project wrote for a person to read.
    """
    if transcript is None:
        # Not reachable from either commit path: a memo arrives here having just
        # been transcribed, or carrying the transcript that let it skip
        # transcription. Handled rather than asserted because the alternative is
        # handing None to an enricher that will call a string method on it.
        return None, None

    try:
        return enricher.enrich(transcript), None
    except EnrichmentError as error:
        log.warning("memo %s: enrichment failed: %s", memo.id, error)

        return None, str(error)
    except Exception:
        log.exception("memo %s: unexpected error while enriching", memo.id)

        return None, UNEXPECTED_ENRICHMENT_ERROR


def job_budget_seconds(max_audio_seconds: float, enricher: Enricher = NO_ENRICHMENT) -> float:
    """
    The longest one job can legitimately take, summed from the deadlines that bound it.

    This is the number the reaper's lease has to exceed. It is derived rather than
    estimated because every term in it is an explicit timeout somewhere in this
    package, and because the sum moves: raising ``MAX_AUDIO_SECONDS`` lengthens the
    decode deadline, so a lease chosen once against the old value silently starts
    reaping healthy jobs. ``memo_ai/worker/__main__`` recomputes this at boot and
    says so in the log rather than leaving that to whoever edits the ``.env``.

    Worst case rather than typical, deliberately -- reaping a live job costs the
    work it was doing, so the bound has to hold for the slowest run that is still
    working correctly, not the median. At the shipped defaults that is 3,300s: 30s
    of ffprobe on the upload, 120s of ffmpeg, 30s of ffprobe on the result, 300s
    waiting for a model to load, 2,400s of decode deadline for a ten-minute
    recording, and the 420s of enrichment below. The real numbers are two orders of
    magnitude smaller.

    The STT terms come from the ``local`` provider, which is the only configured
    provider that can spend real time: ``fake`` is instant, and the hosted adapter
    was deliberately left unwritten (memo_ai/stt/unimplemented.py). Reading one
    provider's constants from here is a bound, not a call path -- the abstraction
    is still intact -- but a provider with a longer one belongs in this sum, and
    that is the note whoever writes it needs.

    **The retry backoff is deliberately not in this sum**, which is worth saying
    because the task that specified the lease phrased it as "``MAX_AUDIO_SECONDS``
    plus backoff". The lease is measured against ``locked_at``, and a memo waiting
    out its backoff is ``queued`` with its lock released -- ``_RETRY`` in
    memo_ai/memos.py is what makes that true. So backoff time is never time spent
    in ``processing``, and adding it here would inflate the lease by up to 90
    seconds of a state the reaper cannot see. What the backoff does bound is how
    long a memo takes end to end, which is a different question and not this one.

    **Enrichment is in this sum as of MEMO-21**, which the previous version of this
    docstring predicted and left undone. It runs between the two commit points,
    inside the same claim, so its deadlines are time the row spends in
    ``processing`` and time the reaper would otherwise count against the lease.

    It is read off the enricher rather than imported from a module, and that is
    what keeps the sum honest under both configurations. ``NoEnrichment`` returns
    immediately and contributes nothing -- it has no ``budget_seconds`` at all,
    which is a stronger statement of "costs no time" than an attribute set to zero
    -- while the local model contributes its load timeout plus its generation
    deadline. So ``ENRICH_PROVIDER=none`` gets the 2,880s bound it had before this
    task, and the shipped configuration gets 3,300s, without either being written
    down twice.
    """
    return (
        # audio.normalize: probe the upload, transcode it, probe the result.
        audio.PROBE_TIMEOUT_SECONDS
        + audio.NORMALIZE_TIMEOUT_SECONDS
        + audio.PROBE_TIMEOUT_SECONDS
        # A cold cache pulls the weights before the first memo can be decoded.
        + local.MODEL_LOAD_TIMEOUT_SECONDS
        # The decode deadline, which local.py scales to the audio's own length.
        + max(
            local.DEADLINE_FLOOR_SECONDS,
            max_audio_seconds * local.DEADLINE_REALTIME_FACTOR,
        )
        # getattr rather than a branch on the type, so an enricher written later
        # opts in by declaring what it can spend -- the same shape the worker uses
        # for the STT providers' optional `prefetch`.
        + float(getattr(enricher, "budget_seconds", 0.0))
    )


@contextmanager
def owed_audio(
    memo: ClaimedMemo,
    provider: SttProvider,
    audio_dir: Path,
    max_audio_seconds: float,
) -> Iterator[audio.NormalizedAudio | None]:
    """
    Yield the normalized audio this memo owes a transcript for, or ``None``.

    ``transcript IS NULL`` is the entire test for whether anything is owed, and it
    is the reason this table needs no second status column and no job type. A text
    memo is inserted with the typed text already in ``transcript`` (MEMO-06) and so
    yields ``None`` here; a voice memo is inserted with NULL and so gets
    normalized and transcribed.

    The same predicate carries a second, stronger property now that the job commits
    twice: a crash after transcription never re-transcribes, because the first
    commit put the text on the row and a re-claim finds it there. That is what makes
    the reaper safe to be aggressive with -- requeueing a half-finished job costs
    the enrichment and never the transcript, and on a hosted provider never a
    second bill.

    A context manager because what it yields is a temporary file. The normalized
    copy is deleted when the caller is done with it, and the original on the
    ``audio`` volume is never touched -- MEMO-23 serves playback from that one.

    Which format gets produced is the provider's choice, defaulting to Opus. See
    ``audio.format_for``.
    """
    if memo.transcript is not None:
        yield None

        return

    if not memo.audio_path:
        # Not reachable from either endpoint that exists today, and worth failing
        # cleanly for anyway: this is the one broken-audio case that can be
        # detected without opening a file, and it is the shape a bad INSERT from a
        # future writer would take.
        raise SttError("This memo owes a transcript but has no audio file recorded against it.")

    source = audio_file(audio_dir, memo.audio_path)

    if not source.is_file():
        # Checked here rather than left to ffmpeg, which reports a missing input
        # by printing the full container path to stderr and exiting 254. The
        # message would be suppressed by memo_ai/audio.py's stderr rule anyway;
        # this way the row says which of the two things went wrong.
        raise SttError("The audio file for this memo is missing from the audio volume.")

    with audio.normalize(source, audio.format_for(provider), max_audio_seconds) as normalized:
        yield normalized


def audio_file(audio_dir: Path, key: str) -> Path:
    """
    Join ``audio_path`` to ``AUDIO_DIR``.

    ``memos.audio_path`` holds a *key* relative to ``AUDIO_DIR``, not an absolute
    path -- that is ``LocalAudioStorage`` on the PHP side, which joins the same key
    to the same root under the same mount. Reading it as absolute would look fine
    in every test and resolve to the container root in production.

    The traversal check mirrors ``LocalAudioStorage::path`` for the reason stated
    there: the key reaches this function from an id the API generated, and "it is
    trusted today" is not a property that survives refactoring. This side has the
    stronger claim on it, because under MEMO-12's uid/gid contract the worker is the
    container that *unlinks* on this volume -- so a key that escaped the root here
    would escape it with delete rights rather than read rights. Which task actually
    performs that unlink is still open: MEMO-12 settled the permissions, and MEMO-23
    adds audio playback, which implies the blobs are kept rather than dropped after
    transcription. Nothing in this file deletes anything today.
    """
    if not key or "\0" in key:
        raise SttError("The audio key on this memo is empty or contains a null byte.")

    parts = PurePosixPath(key).parts

    if key.startswith("/") or ".." in parts:
        raise SttError(f"The audio key {key!r} must be relative and may not traverse upwards.")

    return audio_dir / key

"""
What one job does, between the claim and the result write.

Everything slow lives here, and nothing here holds a transaction open -- that
separation is the design, not an implementation detail. memo_ai/db.py has the
experiment behind it.

Three steps now that MEMO-13 has landed: normalize, measure, transcribe. The
order is the point. The duration comes off the *normalized* file, and it is
checked against ``MAX_AUDIO_SECONDS`` before a provider is called, so a memo that
is too long costs one ffmpeg run and nothing else -- no hosted request, no model
load. memo_ai/audio.py has the reason the duration cannot be read off the
original instead.

The shape is deliberately the one MEMO-16 grows into: it commits once at the end
today, and MEMO-16 splits that into two commits (transcript first, still
``processing``; then enrichment and ``ready``) without moving anything else.
MEMO-21 adds the enrichment call in the gap between them.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from memo_ai import audio
from memo_ai.memos import ClaimedMemo, MemoQueue
from memo_ai.stt.base import SttError, SttProvider

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


def run_job(
    queue: MemoQueue,
    memo: ClaimedMemo,
    provider: SttProvider,
    audio_dir: Path,
    max_audio_seconds: float,
) -> None:
    """
    Do the work the claim promised, then write exactly one result.

    The ``try`` covers the audio work and the transcription and nothing else,
    which is deliberate. Both writes below are outside it, so a database failure
    while recording a result propagates to the loop, which reconnects -- and
    leaves the row in ``processing`` for MEMO-16's reaper, because a result that
    could not be written is exactly the case the reaper exists for. Swallowing it
    here would instead mark the memo done on the strength of a write that did not
    happen.
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
            transcript = None if prepared is None else provider.transcribe(prepared.path)
    except audio.AudioTooLong as error:
        # Before AudioError and SttError, which it subclasses. It is the one
        # failure that carries its own duration, because the duration is what
        # caused it, and the generic handler below would drop that.
        log.info("memo %s: refused for length: %s", memo.id, error)
        queue.fail(memo, str(error), error.duration_ms)

        return
    except SttError as error:
        # Classified, so the message is safe and useful on the row.
        log.warning("memo %s: no transcript: %s", memo.id, error)
        queue.fail(memo, str(error), duration_ms)

        return
    except Exception:
        # log.exception, so the traceback is in the container logs even though the
        # row only carries the generic sentence above.
        log.exception("memo %s: unexpected error while transcribing", memo.id)
        queue.fail(memo, UNEXPECTED_ERROR, duration_ms)

        return

    if queue.finish_ready(memo, transcript, duration_ms):
        log.info(
            "memo %s ready in %.0fms (attempt %d, %s%s)",
            memo.id,
            (time.monotonic() - started) * 1000,
            memo.attempts,
            "transcribed" if transcript else "transcript already present",
            "" if duration_ms is None else f", {duration_ms}ms of audio",
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
    normalized and transcribed. MEMO-16 leans on the same predicate for a stronger
    property: a crash after a paid transcription never re-bills it, because on
    re-claim the transcript is there.

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

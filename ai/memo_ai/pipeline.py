"""
What one job does, between the claim and the result write.

Everything slow lives here, and nothing here holds a transaction open -- that
separation is the design, not an implementation detail. memo_ai/db.py has the
experiment behind it.

The shape is deliberately the one MEMO-16 grows into: it commits once at the end
today, and MEMO-16 splits that into two commits (transcript first, still
``processing``; then enrichment and ``ready``) without moving anything else.
MEMO-21 adds the enrichment call in the gap between them.
"""

import logging
import time
from pathlib import Path, PurePosixPath

from memo_ai.memos import ClaimedMemo, MemoQueue
from memo_ai.stt.base import SttError, SttProvider, Transcript

log = logging.getLogger(__name__)

# What goes in `last_error` when the exception was not one we classified. The real
# detail goes to the log instead.
#
# The reason is that `last_error` is part of the API's response projection, so it
# reaches the browser -- and an arbitrary exception's text is not something this
# code chose. A psycopg connection error, for one, stringifies as `connection to
# server at "db" (172.18.0.2), port 5432 failed: ...`, which puts the internal
# topology in an HTTP response to answer a question the user did not ask. Only
# messages an implementation wrote for this column go in it; see SttError.
UNEXPECTED_ERROR = "Unexpected worker error. See the ai-worker logs for details."


def run_job(
    queue: MemoQueue,
    memo: ClaimedMemo,
    provider: SttProvider,
    audio_dir: Path,
) -> None:
    """
    Do the work the claim promised, then write exactly one result.

    The ``try`` covers the transcription and nothing else, which is deliberate.
    Both writes below are outside it, so a database failure while recording a
    result propagates to the loop, which reconnects -- and leaves the row in
    ``processing`` for MEMO-16's reaper, because a result that could not be written
    is exactly the case the reaper exists for. Swallowing it here would instead
    mark the memo done on the strength of a write that did not happen.
    """
    started = time.monotonic()

    try:
        transcript = transcribe_if_owed(memo, provider, audio_dir)
    except SttError as error:
        # Classified, so the message is safe and useful on the row.
        log.warning("memo %s: transcription failed: %s", memo.id, error)
        queue.fail(memo, str(error))

        return
    except Exception:
        # log.exception, so the traceback is in the container logs even though the
        # row only carries the generic sentence above.
        log.exception("memo %s: unexpected error during transcription", memo.id)
        queue.fail(memo, UNEXPECTED_ERROR)

        return

    if queue.finish_ready(memo, transcript):
        log.info(
            "memo %s ready in %.0fms (attempt %d, %s)",
            memo.id,
            (time.monotonic() - started) * 1000,
            memo.attempts,
            "transcribed" if transcript else "transcript already present",
        )


def transcribe_if_owed(
    memo: ClaimedMemo,
    provider: SttProvider,
    audio_dir: Path,
) -> Transcript | None:
    """
    Transcribe, or return ``None`` when the memo already has its words.

    ``transcript IS NULL`` is the entire test, and it is the reason this table
    needs no second status column and no job type. A text memo is inserted with
    the typed text already in ``transcript`` (MEMO-06) and so returns ``None``
    here; a voice memo is inserted with NULL and so gets transcribed. MEMO-16
    leans on the same predicate for a stronger property: a crash after a paid
    transcription never re-bills it, because on re-claim the transcript is there.
    """
    if memo.transcript is not None:
        return None

    if not memo.audio_path:
        # Not reachable from either endpoint that exists today, and worth failing
        # cleanly for anyway: this is the one broken-audio case that can be
        # detected without opening a file, and it is the shape a bad INSERT from a
        # future writer would take.
        raise SttError("This memo owes a transcript but has no audio file recorded against it.")

    return provider.transcribe(audio_file(audio_dir, memo.audio_path))


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

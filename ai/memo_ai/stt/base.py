"""
The transcription contract. No implementation and no imports beyond the standard
library, so that adding a provider means reading one short file.

MEMO-14 owns the real one (``local``, faster-whisper) and the error classification
that goes with it -- per-call timeout, whole-job deadline, retryable versus
terminal. This file deliberately stops short of that: MEMO-08 needs the seam to
exist and be exercised by something, not to be finished.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SttError(Exception):
    """
    A transcription attempt that produced no transcript.

    ``str()`` of one of these is written to ``memos.last_error``, and that column
    is part of the API's response projection (``MemoRepository::COLUMNS``) -- so it
    reaches the browser. Implementations therefore owe the reader of this class one
    thing: put a sentence a person can act on in the message, and keep connection
    strings, keys and internal paths out of it. memo_ai/pipeline.py is what
    enforces the other half, by refusing to copy an *unclassified* exception's
    text into the row.
    """


class SttUnavailable(SttError):
    """
    The provider cannot run here at all, as opposed to failing on this audio.

    Separate from ``SttError`` because MEMO-14's fallback chain has to tell the two
    apart: "this provider is unusable, try the fallback" is a different decision
    from "this file is corrupt, and the fallback will not do any better".
    """


@dataclass(frozen=True)
class Transcript:
    """
    What a provider returns, and it is not just the text.

    ``provider`` and ``model`` are carried on the result rather than read back off
    the configuration when the row is written, because with MEMO-14's fallback in
    place those two answers diverge: ``STT_PROVIDER`` says what was *asked for* and
    this says what actually produced the words. ``memos.stt_provider`` wants the
    second one.

    No ``duration_ms``. It belongs to the row and MEMO-16 commits it alongside the
    transcript, but it comes from ffprobe (MEMO-13) rather than from a provider --
    and the fake provider inventing a plausible number for audio it never opened
    would put a lie in a column that later gets charged by the minute.
    """

    text: str
    provider: str

    # None means "no model was involved", which is the truth for the fake provider
    # and not the same as "the default model". See fake.py.
    model: str | None = None


class SttProvider(Protocol):
    """
    Structural, not an ABC. A provider is anything with a name and a
    ``transcribe``, so a test double is a five-line class and needs to import
    nothing from here.
    """

    name: str

    def transcribe(self, audio: Path) -> Transcript:
        """
        Turn one audio file into a transcript, or raise ``SttError``.

        ``audio`` is an absolute path inside the container, already joined to
        ``AUDIO_DIR`` and checked for traversal by memo_ai/pipeline.py. A provider
        does not have to trust it exists -- MEMO-14's local provider is expected to
        fail cleanly on a missing or zero-length file rather than hang.
        """
        ...

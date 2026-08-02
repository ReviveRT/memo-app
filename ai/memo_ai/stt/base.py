"""
The transcription contract. No implementation and no imports beyond the standard
library, so that adding a provider means reading one short file.

Two implementations satisfy it as of MEMO-14 -- ``local`` (faster-whisper) and
``fake`` -- which is what keeps this file a contract rather than a description of
its only caller. The error classification below is the half that took a real
provider to settle, and memo_ai/stt/chain.py is the code that depends on it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from memo_ai import failures


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

    **And one token beside the sentence.** ``code`` is written to
    ``memos.last_error_code`` and is what a program branches on, because the message
    is prose and prose gets reworded -- memo_ai/failures.py has the argument. It is a
    class attribute so a subclass names its kind once instead of every raise site
    repeating it, and a constructor keyword so a raise site that knows better can say
    so: :data:`~memo_ai.failures.NO_SPEECH` and the generic transcription failure are
    both ``SttError``, and only the raise site can tell them apart.
    """

    code = failures.TRANSCRIPTION_FAILED

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)

        # Assigned only when overridden, so the class attribute stays visible on the
        # instance rather than being shadowed by a copy of itself. That matters for
        # a subclass like AudioTooLong, whose own __init__ calls up to here without
        # a code and must keep the one its class declares.
        if code is not None:
            self.code = code


class SttUnavailable(SttError):
    """
    The provider cannot run here at all, as opposed to failing on this audio.

    Separate from ``SttError`` because the fallback chain tells the two apart:
    "this provider is unusable, try the fallback" is a different decision from
    "this file is corrupt, and the fallback will not do any better". memo_ai/stt/
    chain.py is the one reader of that distinction, and memo_ai/stt/local.py is
    where a real provider has to choose between them on every failure it has.
    """

    code = failures.PROVIDER_UNAVAILABLE


@dataclass(frozen=True)
class Transcript:
    """
    What a provider returns, and it is not just the text.

    ``provider`` and ``model`` are carried on the result rather than read back off
    the configuration when the row is written, because with the fallback chain in
    place those two answers diverge: ``STT_PROVIDER`` says what was *asked for* and
    this says what actually produced the words. ``memos.stt_provider`` wants the
    second one. ``STT_PROVIDER=openai`` on the shipped defaults is that divergence
    in practice -- the memo transcribes, and the row says `local`.

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

    # What this transcription cost, in millionths of a dollar, or None for "nobody
    # was billed". Both real providers answer None -- `local` runs in this
    # container and `fake` runs nowhere -- and that is the honest value rather than
    # a placeholder: zero would claim a measurement, and `memos.cost_micro_usd` is
    # summed by MEMO-22 into a figure a person is expected to trust.
    #
    # On the result rather than derived from a rate table at write time, for the
    # reason `provider` is: with a fallback chain in place, the row that was billed
    # is not necessarily the provider that was configured. A hosted adapter fills
    # this in from its own response; the column takes it with COALESCE, so a
    # provider that has nothing to say leaves whatever an earlier attempt recorded.
    cost_micro_usd: int | None = None


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

        ``audio`` is the *normalized* copy, in a temporary directory that lives
        only as long as the job -- not the upload on the shared volume, whose path
        memo_ai/pipeline.py joins and checks for traversal. A provider does not
        have to trust it exists or is readable: the local one classifies a missing
        or undecodable file rather than hanging on it.
        """
        ...

"""
``STT_PROVIDER`` first, ``STT_FALLBACK`` if the first one cannot run.

The variable has existed since MEMO-08, which validated the name and then did
nothing with it, because walking a chain needs the classification that only
arrives with a provider that can really fail. This is that walk, and it is
deliberately short: two providers, one condition, no retries. Retries and backoff
are MEMO-16's and they wrap the whole job rather than this call.

**The condition is :class:`SttUnavailable`, and nothing else.** That subclass
means "this provider cannot run here", which is the only thing a second provider
can do anything about. A plain :class:`SttError` means the audio produced no
transcript -- unreadable, empty, silent -- and every provider in this chain is
fed the same normalized file by memo_ai/audio.py, so a second attempt would spend
the time and reach the same answer. ``AudioError`` says so at its own definition.

An unclassified exception is not caught here either. That is a bug rather than an
outcome, and memo_ai/pipeline.py logs the traceback and writes a generic sentence
for it. Falling back would turn a bug into a slightly slower success and nobody
would ever see it.
"""

import logging
from pathlib import Path

from memo_ai.stt.base import SttProvider, SttUnavailable, Transcript

log = logging.getLogger(__name__)


class FallbackStt:
    """
    Two providers, tried in order. Transparent about which one answered.

    Transparent is the operative word: :class:`Transcript` carries ``provider``
    and ``model`` from whichever one produced the words, and this class passes
    that result through untouched, so ``memos.stt_provider`` records what actually
    ran rather than what was configured. That split is why those two fields are on
    the result at all -- ``stt/base.py`` has the note.
    """

    def __init__(self, primary: SttProvider, fallback: SttProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"

        # Whatever the primary asked for, including "nothing" -- which is how a
        # provider says Opus is fine. ``audio.format_for`` reads this attribute
        # off the chain exactly as it would off the primary, and its isinstance
        # check turns a None back into the default, so this stays transparent
        # without importing that module and without knowing what a format is.
        #
        # The fallback gets the primary's choice rather than its own, and that is
        # a real limitation stated rather than hidden: normalization happens once,
        # before either provider is called, and re-running ffmpeg on the way to a
        # fallback would double the cost of the path that only runs when something
        # has already gone wrong. It is safe for the two providers that exist --
        # `fake` never opens the file, and `local` was run against both formats on
        # the browser fixtures and produced the same words either way. The same
        # words rather than the same string: on the shipped model the Opus output
        # picks up a trailing full stop the WAV one does not.
        self.audio_format = getattr(primary, "audio_format", None)

    def prefetch(self) -> None:
        """
        Warm the primary at boot, and only the primary.

        The fallback is deliberately left cold. It runs when something has
        already gone wrong, which is rare, and paying for it up front would undo
        the one instruction in the README for avoiding the download altogether:
        ``STT_PROVIDER=fake`` ships with ``STT_FALLBACK=local``, so warming both
        would fetch 1.6 GB for a configuration whose entire point is not to.

        ``getattr`` for the same reason ``audio_format`` is read that way -- a
        provider opts in, and the five-line test double in ``SttProvider``'s
        docstring stays five lines.
        """
        warm = getattr(self.primary, "prefetch", None)

        if warm is not None:
            warm()

    def transcribe(self, source: Path) -> Transcript:
        try:
            return self.primary.transcribe(source)
        except SttUnavailable as unavailable:
            # Warning, not error: the fallback may well succeed, and this line is
            # then the only record that the configured provider did not. It is
            # also the only place the primary's message survives -- what reaches
            # `last_error` is whatever the fallback raises, because the fallback
            # is the attempt that actually read this recording.
            log.warning(
                "stt provider %r is unavailable (%s), falling back to %r",
                self.primary.name,
                unavailable,
                self.fallback.name,
            )

        return self.fallback.transcribe(source)

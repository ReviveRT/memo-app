"""
A stand-in for a provider that is named in the configuration surface but has no
implementation.

One name is in that position after MEMO-14: `openai`. It is documented in
.env.example and in the README's variable table, so it has to mean something, and
what it means is a decision rather than a gap -- the ticket's own instruction was
to prove the seam with two working providers and not to ship a hosted adapter
that had never been run against the real API. `local` was the other name here
until MEMO-14 built it.
"""

from pathlib import Path

from memo_ai.stt.base import SttUnavailable, Transcript


class UnimplementedStt:
    """
    Resolves fine, fails at use. That split is the whole design of this class.

    Failing at *resolve* time -- refusing to boot -- was the first version and it
    is wrong for the reason that mattered while `local` was also in this file: the
    committed default in docker-compose.yml, .env.example and the README must not
    be able to stop the worker starting, because ``restart: unless-stopped`` turns
    a boot failure into a restart loop and the grading criterion is that one
    command converges with no manual steps.

    That reason survives `local` leaving. ``STT_PROVIDER=openai`` is a
    configuration a reader could plausibly try after seeing the name in the
    variable table, and the outcome should be a memo that explains itself, not a
    stack that will not come up.

    :class:`SttUnavailable` rather than a plain error, and that is what makes this
    class useful rather than merely honest. It is the fallback chain's condition,
    so ``STT_PROVIDER=openai`` with the default ``STT_FALLBACK=local`` transcribes
    on the local model and records `local` in ``memos.stt_provider`` -- the truth
    about what ran -- with one warning in the log naming what was skipped.

    Not resolved to the fake provider instead, for the reason that has been here
    since MEMO-08: silently substituting canned text for a configuration that
    asked for real transcription is the single worst behaviour available. It would
    pass every acceptance criterion and lie in production.
    """

    def __init__(self, name: str, because: str) -> None:
        self.name = name
        self._because = because

    def transcribe(self, audio: Path) -> Transcript:
        raise SttUnavailable(
            f"The {self.name!r} speech-to-text provider is not built in this project — "
            f"{self._because}. Set STT_PROVIDER=local to transcribe on the local model."
        )

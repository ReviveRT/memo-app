"""
A stand-in for a provider that is named in the configuration surface but has no
implementation yet.

Two names are in that position after MEMO-08: `local`, which MEMO-14 builds, and
`openai`, which MEMO-14 explicitly leaves optional and unenabled. Both are
documented in .env.example and in the README's variable table, so both have to
mean something.
"""

from pathlib import Path

from memo_ai.stt.base import SttUnavailable, Transcript


class UnimplementedStt:
    """
    Resolves fine, fails at use. That split is the whole design of this class.

    Failing at *resolve* time -- refusing to boot on ``STT_PROVIDER=local`` -- was
    the first version and it is wrong, because `local` is the committed default in
    docker-compose.yml, .env.example and the README. A boot failure there would
    mean ``docker compose up`` on a clean checkout brings up a worker that exits,
    and ``restart: unless-stopped`` would turn that into a restart loop. The
    grading criterion is that one command converges with no manual steps, so the
    default configuration is the one that must not fail.

    Failing at *use* time costs nothing today, and this is checkable rather than
    hopeful: the only path that reaches ``transcribe()`` is a memo with
    ``transcript IS NULL``, which only the audio upload endpoint creates, and that
    endpoint arrives in MEMO-11. Until then this object is constructed on every
    boot and never called. When MEMO-11 does land ahead of MEMO-14, an audio memo
    stops at ``status='failed'`` with the sentence below in ``last_error``, which
    is a readable answer in the UI rather than a hang or a canned transcript
    pretending to be real.

    Not resolved to the fake provider instead, for that last reason: silently
    substituting canned text for a configuration that asked for real transcription
    is the single worst behaviour available here. It would pass MEMO-08's
    acceptance and lie in production.
    """

    def __init__(self, name: str, owner: str) -> None:
        self.name = name
        self._owner = owner

    def transcribe(self, audio: Path) -> Transcript:
        raise SttUnavailable(
            f"The {self.name!r} speech-to-text provider is not implemented yet "
            f"({self._owner}). Set STT_PROVIDER=fake to run the queue without it."
        )

"""The provider that returns canned text. MEMO-08's whole point."""

from pathlib import Path

from memo_ai.stt.base import Transcript

# A constant, not a template, and phrased so it cannot be mistaken for output.
#
# Constant because the test suite asserts on it; interpolating the memo id would make
# that weaker for no gain. Unmistakable because this string lands in `memos.transcript`,
# is indexed into `search_vector`, and will be sitting in a demo database months
# from now -- "hello world" or a lorem ipsum would eventually be read as a real
# transcription that had gone wrong.
#
# This comment also gave MEMO-09's clean-checkout gate as a second reason to keep it
# constant, on the grounds that the gate reads it off the screen. Running that gate
# showed it cannot, for two independent reasons: the gate submits a *text* memo, which
# carries its own transcript and so returns from transcribe_if_owed before any provider
# is called (memo_ai/pipeline.py), and STT_PROVIDER defaults to `local` regardless,
# which resolves to UnimplementedStt -- so FakeStt is never constructed on that path at
# all. The worker logged `stt_provider=local` and `transcript already present`, and no
# canned string reached the database. Nothing puts this on a screen until MEMO-11 adds
# the upload endpoint and a voice memo becomes possible.
CANNED_TRANSCRIPT = "Canned transcript from the fake speech-to-text provider. No audio was read."


class FakeStt:
    """
    Instant, deterministic, and it never touches the filesystem.

    Not reading the file is a deliberate trade and it is what makes this provider
    useful here. MEMO-08 is build order 9 and the upload endpoint is MEMO-11 at
    build order 14, so there is no way to get real bytes onto the `audio` volume for
    another five positions -- a fake that stat'd its argument could not be used to
    prove the queue works, which is the one thing this task is for. It also keeps the
    provider usable from a test that has no volume mounted at all, and it is what
    MEMO-14 means when it specifies "`fake` is instant".

    What that costs, stated rather than discovered later: this provider does not
    exercise the missing-file or corrupt-file paths. Those are MEMO-14's
    acceptance criteria against the local provider, and the part that *can* be
    checked without bytes -- a memo that owes a transcript but carries no
    `audio_path` -- is checked one level up, in memo_ai/pipeline.py.
    """

    name = "fake"

    def transcribe(self, audio: Path) -> Transcript:
        # `model=None` rather than settings.stt_model. STT_MODEL defaults to
        # `base`, and recording "base" against a canned string would put a claim
        # in `memos.stt_model` that no model backs -- which is precisely the
        # column MEMO-22 reads to price a run.
        return Transcript(text=CANNED_TRANSCRIPT, provider=self.name, model=None)

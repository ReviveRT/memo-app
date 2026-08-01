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
# showed it cannot: the gate submits a *text* memo, which carries its own transcript
# and so returns from owed_audio before any provider is called (memo_ai/pipeline.py),
# and STT_PROVIDER defaults to `local` regardless -- so FakeStt is never constructed on
# that path at all. The worker logged `transcript already present`, and no canned
# string reached the database. What does put it on a screen is MEMO-11's upload
# endpoint plus an explicit `STT_PROVIDER=fake`, which is a thing a reader is told to
# try in the README.
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

    What that costs is smaller than it was. This provider still does not exercise
    the missing-file or corrupt-file paths, but as of MEMO-13 nothing needs it to:
    normalization runs *before* whichever provider is configured, so a missing
    blob, a file ffmpeg cannot decode, and a video with no audio track are all
    caught in memo_ai/audio.py and never reach here. That is true on
    `STT_PROVIDER=fake` too, which makes the fake a fair rehearsal of the queue
    rather than a path that skips the checks.

    The consequence worth stating: a voice memo on the fake provider now does real
    work -- three ffmpeg-family processes -- before it gets its canned sentence. It
    is no longer instant in the way MEMO-08 meant, though it is still the fastest
    provider by a wide margin, because it is the transcription that is free rather
    than the pipeline.
    """

    name = "fake"

    def transcribe(self, audio: Path) -> Transcript:
        # `model=None` rather than settings.stt_model. STT_MODEL defaults to
        # `base`, and recording "base" against a canned string would put a claim
        # in `memos.stt_model` that no model backs -- which is precisely the
        # column MEMO-22 reads to price a run.
        return Transcript(text=CANNED_TRANSCRIPT, provider=self.name, model=None)

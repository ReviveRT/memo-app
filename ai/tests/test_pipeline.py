"""
What one job decides: whether transcription is owed, what the provider is handed,
and which write ends it.

None of this needs a database and none of it needs ffmpeg. The claim and the fence
need Postgres and were checked against a real one instead -- see memo_ai/memos.py
for what those runs showed. Normalization needs ffmpeg and is checked against a
real one in tests/test_audio.py; here it is stubbed, so that what fails in this
file is a decision rather than a transcode.
"""

from pathlib import Path

import pytest

from memo_ai import audio, pipeline
from memo_ai.stt.base import SttError, SttUnavailable
from tests.support import FakeQueue, RecordingNormalizer, RecordingStt, claimed_memo

MAX_SECONDS = 600.0


@pytest.fixture
def audio_dir(tmp_path):
    """A real ``AUDIO_DIR`` holding the blob ``claimed_memo`` points at."""
    root = tmp_path / "audio"
    (root / "2026" / "07" / "31").mkdir(parents=True)
    (root / "2026" / "07" / "31" / "memo.webm").write_bytes(b"pretend this is a recording")

    return root


@pytest.fixture
def normalizer(monkeypatch):
    stub = RecordingNormalizer()
    monkeypatch.setattr(audio, "normalize", stub)

    return stub


def run(queue, memo, provider, audio_dir, max_seconds=MAX_SECONDS):
    pipeline.run_job(queue, memo, provider, audio_dir, max_seconds)


def test_a_text_memo_is_not_normalized_or_transcribed_and_goes_straight_to_ready(
    audio_dir, normalizer
):
    # `transcript IS NULL` is the whole test for "does this memo owe a
    # transcript", which is why the table needs no job type and no second status
    # column. A text memo arrives with the typed text already in place (MEMO-06),
    # so it never reaches ffmpeg -- which is what lets a worker with no ffmpeg
    # still drain half the queue.
    provider = RecordingStt()
    queue = FakeQueue()
    memo = claimed_memo(source="text", transcript="the typed text", audio_path=None)

    run(queue, memo, provider, audio_dir)

    assert provider.calls == []
    assert normalizer.calls == []
    assert queue.failed == []
    assert queue.finished == [(memo, None)]
    # No audio, so no length. The same NULL the row was inserted with.
    assert queue.durations == [None]


def test_a_voice_memo_is_normalized_from_audio_dir_joined_to_the_key(audio_dir, normalizer):
    # audio_path is a *key* relative to AUDIO_DIR, the same key LocalAudioStorage
    # wrote it under. Reading it as an absolute path would look fine here and
    # resolve to the container root in production.
    run(FakeQueue(), claimed_memo(audio_path="2026/07/31/memo.webm"), RecordingStt(), audio_dir)

    assert normalizer.calls[0].source == audio_dir / "2026/07/31/memo.webm"
    assert normalizer.calls[0].max_seconds == MAX_SECONDS


def test_the_provider_is_handed_the_normalized_copy_not_the_original(audio_dir, normalizer):
    # The whole point of the module: one decode path, so a provider never sees
    # Chrome WebM, Firefox Ogg and Safari MP4 as three different problems.
    provider = RecordingStt(text="spoken words")
    queue = FakeQueue()

    run(queue, claimed_memo(), provider, audio_dir)

    given = provider.calls[0]

    assert given != audio_dir / "2026/07/31/memo.webm"
    assert given.suffix == audio.OPUS.suffix

    _, transcript = queue.finished[0]

    assert transcript is not None
    assert transcript.text == "spoken words"


def test_the_measured_duration_is_written_with_the_transcript(audio_dir, normalizer):
    normalizer.duration_ms = 7_314
    queue = FakeQueue()

    run(queue, claimed_memo(), RecordingStt(), audio_dir)

    assert queue.durations == [7_314]


def test_the_normalized_copy_is_gone_once_the_job_is_over(audio_dir, normalizer):
    # It is a temporary derivative. A worker runs for weeks and would otherwise
    # accumulate one of these per voice memo.
    run(FakeQueue(), claimed_memo(), RecordingStt(), audio_dir)

    assert normalizer.yielded and not normalizer.yielded[0].exists()


def test_a_provider_gets_opus_unless_it_asks_for_something_else(audio_dir, normalizer):
    # Opus is the default because WAV is larger than the browser's own recording
    # and would eat most of a hosted request limit. A provider that decodes
    # in-process -- MEMO-14's faster-whisper -- opts out by declaring a format.
    run(FakeQueue(), claimed_memo(), RecordingStt(), audio_dir)

    assert normalizer.calls[0].format == audio.OPUS

    wants_wav = RecordingStt()
    wants_wav.audio_format = audio.WAV

    run(FakeQueue(), claimed_memo(), wants_wav, audio_dir)

    assert normalizer.calls[1].format == audio.WAV


def test_audio_over_the_cap_fails_before_the_provider_is_called(audio_dir, normalizer):
    # The reason the cap is checked here rather than after transcription: a memo
    # that is too long must not cost a hosted request or a model load.
    normalizer.error = audio.AudioTooLong("This recording is 11:04 long...", 664_200)
    provider = RecordingStt()
    queue = FakeQueue()

    run(queue, claimed_memo(), provider, audio_dir)

    assert provider.calls == []
    assert queue.finished == []
    assert queue.failed[0][1] == "This recording is 11:04 long..."
    # The number the sentence is about, on the row beside it.
    assert queue.durations == [664_200]


def test_a_transcription_failure_after_measuring_still_records_the_length(audio_dir, normalizer):
    # The duration was measured before the provider ran and is true regardless of
    # what the provider then did. Dropping it would leave a failed memo showing a
    # blank length in the UI MEMO-17 builds.
    normalizer.duration_ms = 4_200
    provider = RecordingStt(error=SttUnavailable("The 'local' provider is not implemented yet."))
    queue = FakeQueue()

    run(queue, claimed_memo(), provider, audio_dir)

    assert queue.failed[0][1] == "The 'local' provider is not implemented yet."
    assert queue.durations == [4_200]


def test_a_memo_owing_a_transcript_with_no_audio_fails_cleanly(audio_dir, normalizer):
    # Unreachable from either endpoint that exists today. Covered because it is the
    # one broken-audio case detectable without opening a file, and because it is the
    # shape a bad INSERT from a future writer would take.
    provider = RecordingStt()
    queue = FakeQueue()

    run(queue, claimed_memo(audio_path=None), provider, audio_dir)

    assert provider.calls == []
    assert normalizer.calls == []
    assert queue.finished == []
    assert "no audio file" in queue.failed[0][1]
    assert queue.durations == [None]


def test_a_key_pointing_at_a_blob_that_is_not_there_says_so(audio_dir, normalizer):
    # Checked before ffmpeg, which reports a missing input by printing the full
    # container path to stderr -- suppressed by the stderr rule in memo_ai/audio.py,
    # which would leave the row saying the file could not be *decoded*.
    queue = FakeQueue()

    run(queue, claimed_memo(audio_path="2026/07/31/gone.webm"), RecordingStt(), audio_dir)

    assert normalizer.calls == []
    assert "missing from the audio volume" in queue.failed[0][1]


def test_a_classified_stt_failure_puts_its_own_message_on_the_row(audio_dir, normalizer):
    queue = FakeQueue()
    provider = RecordingStt(error=SttUnavailable("The 'local' provider is not implemented yet."))

    run(queue, claimed_memo(), provider, audio_dir)

    assert queue.finished == []
    assert queue.failed[0][1] == "The 'local' provider is not implemented yet."


def test_an_unclassified_failure_is_logged_but_not_copied_onto_the_row(audio_dir, normalizer):
    # `last_error` is part of the API's response projection, so it reaches the
    # browser. An arbitrary exception's text is not something this code chose --
    # psycopg's connection errors, for one, carry the container's address and port.
    # Only messages written for that column go in it.
    queue = FakeQueue()
    secret = 'connection to server at "db" (172.18.0.2), port 5432 failed'
    provider = RecordingStt(error=RuntimeError(secret))

    run(queue, claimed_memo(), provider, audio_dir)

    assert queue.finished == []
    assert queue.failed[0][1] == pipeline.UNEXPECTED_ERROR
    assert secret not in queue.failed[0][1]


def test_a_database_failure_while_writing_the_result_is_not_swallowed(audio_dir, normalizer):
    # The row is left in `processing` on purpose: a result that could not be
    # written is precisely the case MEMO-16's reaper exists for. Marking the memo
    # done on the strength of a write that did not happen is the alternative.
    class ExplodingQueue(FakeQueue):
        def finish_ready(self, memo, transcript, duration_ms=None):
            raise ConnectionError("the connection is closed")

    with pytest.raises(ConnectionError):
        run(ExplodingQueue(), claimed_memo(), RecordingStt(), audio_dir)


@pytest.mark.parametrize("key", ["../../etc/passwd", "/etc/passwd", "a/../../b", ""])
def test_an_audio_key_that_escapes_audio_dir_is_refused(key):
    # Mirrors LocalAudioStorage::path on the PHP side, and this side has the
    # stronger claim on it: from MEMO-16 the worker is also what deletes these
    # files. The key comes from an id the API generated, and "it is trusted today"
    # is not a property that survives refactoring.
    with pytest.raises(SttError):
        pipeline.audio_file(Path("/data/audio"), key)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("memo.webm", "/data/audio/memo.webm"),
        ("2026/07/31/memo.webm", "/data/audio/2026/07/31/memo.webm"),
        # Not traversal: a dotted directory name and a file whose stem ends in a
        # dot are both legal keys, and a check written with `".." in key` rather
        # than against the path components would reject them.
        ("a..b/memo.webm", "/data/audio/a..b/memo.webm"),
        ("2026/..7/memo.webm", "/data/audio/2026/..7/memo.webm"),
    ],
)
def test_a_legal_audio_key_joins_to_audio_dir(key, expected):
    assert pipeline.audio_file(Path("/data/audio"), key) == Path(expected)

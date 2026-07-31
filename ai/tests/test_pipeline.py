"""
What one job decides: whether transcription is owed, and which write ends it.

None of this needs a database. The claim and the fence do, and they were checked
against a real Postgres instead -- see memo_ai/memos.py for what those runs showed.
"""

from pathlib import Path

import pytest

from memo_ai import pipeline
from memo_ai.stt.base import SttError, SttUnavailable
from tests.support import FakeQueue, RecordingStt, claimed_memo


def test_a_text_memo_is_not_transcribed_and_goes_straight_to_ready():
    # `transcript IS NULL` is the whole test for "does this memo owe a
    # transcript", which is why the table needs no job type and no second status
    # column. A text memo arrives with the typed text already in place (MEMO-06).
    provider = RecordingStt()
    queue = FakeQueue()
    memo = claimed_memo(source="text", transcript="the typed text", audio_path=None)

    pipeline.run_job(queue, memo, provider, Path("/data/audio"))

    assert provider.calls == []
    assert queue.failed == []
    assert queue.finished == [(memo, None)]


def test_a_voice_memo_is_transcribed_from_audio_dir_joined_to_the_key():
    # audio_path is a *key* relative to AUDIO_DIR, the same key LocalAudioStorage
    # wrote it under. Reading it as an absolute path would look fine here and
    # resolve to the container root in production.
    provider = RecordingStt(text="spoken words")
    queue = FakeQueue()
    memo = claimed_memo(audio_path="2026/07/31/memo.webm")

    pipeline.run_job(queue, memo, provider, Path("/data/audio"))

    assert provider.calls == [Path("/data/audio/2026/07/31/memo.webm")]

    written_memo, transcript = queue.finished[0]

    assert written_memo is memo
    assert transcript is not None
    assert transcript.text == "spoken words"
    assert transcript.provider == "recording"


def test_a_memo_owing_a_transcript_with_no_audio_fails_cleanly():
    # Unreachable from either endpoint that exists today. Covered because it is the
    # one broken-audio case detectable without opening a file, and because it is the
    # shape a bad INSERT from a future writer would take.
    provider = RecordingStt()
    queue = FakeQueue()

    pipeline.run_job(queue, claimed_memo(audio_path=None), provider, Path("/data/audio"))

    assert provider.calls == []
    assert queue.finished == []
    assert "no audio file" in queue.failed[0][1]


def test_a_classified_stt_failure_puts_its_own_message_on_the_row():
    queue = FakeQueue()
    provider = RecordingStt(error=SttUnavailable("The 'local' provider is not implemented yet."))

    pipeline.run_job(queue, claimed_memo(), provider, Path("/data/audio"))

    assert queue.finished == []
    assert queue.failed[0][1] == "The 'local' provider is not implemented yet."


def test_an_unclassified_failure_is_logged_but_not_copied_onto_the_row():
    # `last_error` is part of the API's response projection, so it reaches the
    # browser. An arbitrary exception's text is not something this code chose --
    # psycopg's connection errors, for one, carry the container's address and port.
    # Only messages written for that column go in it.
    queue = FakeQueue()
    secret = 'connection to server at "db" (172.18.0.2), port 5432 failed'
    provider = RecordingStt(error=RuntimeError(secret))

    pipeline.run_job(queue, claimed_memo(), provider, Path("/data/audio"))

    assert queue.finished == []
    assert queue.failed[0][1] == pipeline.UNEXPECTED_ERROR
    assert secret not in queue.failed[0][1]


def test_a_database_failure_while_writing_the_result_is_not_swallowed():
    # The row is left in `processing` on purpose: a result that could not be
    # written is precisely the case MEMO-16's reaper exists for. Marking the memo
    # done on the strength of a write that did not happen is the alternative.
    class ExplodingQueue(FakeQueue):
        def finish_ready(self, memo, transcript):
            raise ConnectionError("the connection is closed")

    with pytest.raises(ConnectionError):
        pipeline.run_job(ExplodingQueue(), claimed_memo(), RecordingStt(), Path("/data/audio"))


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

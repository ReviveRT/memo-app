"""
Test doubles and row factories.

The queue and connection fakes are the Python counterpart of
``api/tests/Support/FakeMemoRepository.php``, and they exist for the same reason:
every statement in memo_ai/memos.py is Postgres-specific, so there is no in-memory
database that would run them honestly. What these doubles cover is the decisions
made *around* the SQL -- which statement runs, what parameters it is given, what
the code does when the fence loses. Whether the statements themselves are correct
was settled against a real Postgres instead.
"""

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from memo_ai.audio import AudioFormat, NormalizedAudio
from memo_ai.enrich import Enrichment
from memo_ai.memos import ClaimedMemo, Reaped, RetryPolicy
from memo_ai.stt.base import Transcript

# A fixed claim token with a full six digits of microseconds, because that is the
# precision `timestamptz` carries and the fence compares on.
LOCKED_AT = datetime(2026, 7, 31, 12, 0, 0, 123456, tzinfo=UTC)

# The shipped policy, spelled out rather than read from Settings.from_env: a test
# that asserts "the third attempt is the last one" should fail when somebody
# changes the default, not silently follow it.
POLICY = RetryPolicy(max_attempts=3, backoff_seconds=30.0, reap_after_seconds=3600.0)


def claimed_memo(**overrides) -> ClaimedMemo:
    """A voice memo owing a transcript, which is the interesting default."""
    fields = {
        "id": UUID("01900000-0000-7000-8000-000000000001"),
        "source": "voice",
        "transcript": None,
        "audio_path": "2026/07/31/memo.webm",
        "attempts": 1,
        "locked_at": LOCKED_AT,
    }

    return ClaimedMemo(**(fields | overrides))


class RecordingStt:
    """An STT provider that records its calls and returns or raises what it is told."""

    name = "recording"

    def __init__(self, text: str = "transcribed", error: Exception | None = None) -> None:
        self.calls: list[Path] = []
        self._text = text
        self._error = error

    def transcribe(self, audio: Path) -> Transcript:
        self.calls.append(audio)

        if self._error is not None:
            raise self._error

        return Transcript(text=self._text, provider=self.name, model="stub-1")


@dataclass(frozen=True)
class NormalizeCall:
    """One recorded call to the stubbed ``audio.normalize``."""

    source: Path
    format: AudioFormat
    max_seconds: float


class RecordingNormalizer:
    """
    Stands in for ``audio.normalize``, so the pipeline tests need no ffmpeg.

    What it preserves from the real thing is everything the pipeline can observe:
    it yields a path *different* from the source, so a test can prove the provider
    was handed the normalized copy rather than the original, and it deletes that
    path on exit, so the cleanup contract is exercised rather than assumed.

    ``duration_ms`` and ``error`` are public and settable, because most tests care
    about exactly one of them and constructing a variant per case reads worse.
    """

    def __init__(self, duration_ms: int = 3_400) -> None:
        self.calls: list[NormalizeCall] = []
        self.yielded: list[Path] = []
        self.duration_ms = duration_ms
        self.error: Exception | None = None

    @contextmanager
    def __call__(self, source: Path, fmt: AudioFormat, max_seconds: float):
        self.calls.append(NormalizeCall(source, fmt, max_seconds))

        if self.error is not None:
            raise self.error

        with tempfile.TemporaryDirectory(prefix="memo-test-normalize-") as scratch:
            path = Path(scratch) / f"normalized{fmt.suffix}"
            path.write_bytes(b"normalized audio")
            self.yielded.append(path)

            yield NormalizedAudio(path=path, duration_ms=self.duration_ms, format=fmt)


class RecordingEnricher:
    """An enricher that records its calls and returns or raises what it is told."""

    name = "recording"

    def __init__(
        self,
        enrichment: Enrichment | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._enrichment = enrichment
        self._error = error

    def enrich(self, transcript: str) -> Enrichment | None:
        self.calls.append(transcript)

        if self._error is not None:
            raise self._error

        return self._enrichment


class FakeQueue:
    """
    Stands in for MemoQueue, recording what the pipeline chose to write.

    Three lists now rather than two, one per write the pipeline can make, because
    which of them happened is the assertion in most of these tests -- a memo that
    reached ``finished`` without passing through ``committed`` would be a memo
    published with an uncommitted transcript.

    ``durations`` is kept beside them rather than added to their tuples, so that
    the assertions about *which* write happened stay readable in tests that do not
    care about the length. The tests that do care read it by index. ``titled_from``
    is beside them for the same reason -- it records the transcript the pipeline
    handed to commit 2 for the fallback title, which only one test asks about.
    """

    def __init__(self, fence_holds: bool = True, transcript_fence_holds: bool = True) -> None:
        self.committed: list[tuple[ClaimedMemo, Transcript]] = []
        self.finished: list[tuple[ClaimedMemo, Enrichment | None, str | None]] = []
        self.failed: list[tuple[ClaimedMemo, str, bool]] = []

        # The failure kind the pipeline passed for each entry in `failed`, same index.
        # Beside rather than inside for the reason `durations` is: most of these tests are
        # about which write happened, not about how it was classified.
        self.codes: list[str] = []
        self.durations: list[int | None] = []
        self.titled_from: list[str | None] = []
        self._fence_holds = fence_holds
        # Separate, so a test can lose the fence on the first commit alone and
        # prove the second one never runs.
        self._transcript_fence_holds = transcript_fence_holds

    def commit_transcript(
        self,
        memo: ClaimedMemo,
        transcript: Transcript,
        duration_ms: int | None = None,
    ) -> bool:
        self.committed.append((memo, transcript))
        self.durations.append(duration_ms)

        return self._fence_holds and self._transcript_fence_holds

    def finish_ready(
        self,
        memo: ClaimedMemo,
        enrichment: Enrichment | None = None,
        enrichment_error: str | None = None,
        text: str | None = None,
    ) -> bool:
        self.finished.append((memo, enrichment, enrichment_error))
        self.titled_from.append(text)

        return self._fence_holds

    def fail_or_retry(
        self,
        memo: ClaimedMemo,
        error: str,
        *,
        code: str,
        retryable: bool,
        duration_ms: int | None = None,
    ) -> bool:
        self.failed.append((memo, error, retryable))

        # Recorded separately rather than added to the tuple above, so the assertions that
        # only care about the sentence and the retry decision -- which is most of them --
        # did not all have to change to gain a field they do not read.
        self.codes.append(code)
        self.durations.append(duration_ms)

        return self._fence_holds

    def reap(self) -> Reaped:
        return Reaped(requeued=[], failed=[], salvaged=[])


class FakeCursor:
    """
    Enough of a psycopg cursor for MemoQueue: a context manager, an ``execute``
    that records, a settable ``rowcount``, a ``fetchone`` and a ``fetchall``.
    """

    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self.rowcount = -1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        self._connection.executed.append((sql, params))
        self.rowcount = self._connection.rowcount

    def fetchone(self):
        return self._connection.row

    def fetchall(self):
        # One list per statement, popped in order, so a reaper test can give the
        # three statements three different answers. Empty when the test did not
        # care, which is the ordinary reaper pass.
        if not self._connection.rows:
            return []

        return self._connection.rows.pop(0)


class FakeConnection:
    """Records every statement and hands back a canned rowcount, row or result set."""

    def __init__(self, rowcount: int = 1, row=None, rows: list[list] | None = None) -> None:
        self.executed: list[tuple[str, dict[str, object] | None]] = []
        self.rowcount = rowcount
        self.row = row
        self.rows = [] if rows is None else list(rows)

    def cursor(self, row_factory=None) -> FakeCursor:
        # row_factory is accepted and ignored: MemoQueue.claim passes class_row,
        # and what these tests check is the parameters and the rowcount handling,
        # not psycopg's row mapping.
        return FakeCursor(self)

    @property
    def last_params(self) -> dict[str, object] | None:
        return self.executed[-1][1]

    @property
    def last_sql(self) -> str:
        return self.executed[-1][0]

    def params_for(self, fragment: str) -> dict[str, object] | None:
        """The parameters of the one executed statement containing ``fragment``."""
        matches = [params for sql, params in self.executed if fragment in sql]

        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one statement containing {fragment!r}, found {len(matches)}"
            )

        return matches[0]

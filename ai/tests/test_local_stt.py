"""
The local provider's decisions, driven through a stubbed model.

Every test here injects a loader, so none of them import faster-whisper, download
a weight or run inference. That is the point rather than a shortcut: what this
file is checking is the classification -- which failures are ``SttUnavailable``
and which are terminal, what the deadline does, what an empty transcript becomes
-- and those are decisions this module makes, not the library's. Whether the
library transcribes correctly is settled in tests/test_local_whisper.py, against
real recordings.
"""

import threading
import time
from pathlib import Path

import pytest

from memo_ai import audio
from memo_ai.stt.base import SttError, SttUnavailable
from memo_ai.stt.local import (
    DEADLINE_FLOOR_SECONDS,
    VAD_SPEECH_PAD_MS,
    LocalWhisperStt,
    _deadline_seconds,
)

AUDIO = Path("/tmp/normalized.wav")


class StubSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class StubInfo:
    def __init__(self, duration: float) -> None:
        self.duration = duration
        self.duration_after_vad = duration


class StubModel:
    """
    Stands in for ``WhisperModel``, yielding segments lazily like the real one.

    Lazy matters: the deadline is enforced *between* segments, so a generator that
    materialised its list up front would make the deadline test pass without
    exercising anything. ``pause`` is what a slow decode looks like from here.
    """

    def __init__(self, texts=("hello ", "world"), duration=5.0, pause=0.0, raises=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._texts = texts
        self._duration = duration
        self._pause = pause
        self._raises = raises

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))

        if self._raises is not None:
            raise self._raises

        return self._segments(), StubInfo(self._duration)

    def _segments(self):
        for text in self._texts:
            if self._pause:
                time.sleep(self._pause)

            yield StubSegment(text)


def provider(model=None, language=None, **kwargs) -> LocalWhisperStt:
    return LocalWhisperStt("base", language, loader=lambda size: model or StubModel(**kwargs))


def test_it_transcribes_and_reports_the_model_that_did_it():
    result = provider(StubModel(texts=("one ", "two"))).transcribe(AUDIO)

    assert result.text == "one two"
    assert result.provider == "local"
    # Not None, unlike the fake provider: a real model ran, and `memos.stt_model`
    # is the column MEMO-22 prices a run from.
    assert result.model == "base"


def test_it_asks_for_wav_and_switches_the_voice_filter_on():
    model = StubModel()
    local = provider(model)
    local.transcribe(AUDIO)

    # WAV because this provider decodes in-process; the filter because whisper
    # invents text over silence, and because an empty transcript is only
    # detectable once the silence has been cut out.
    assert audio.format_for(local) is audio.WAV
    assert model.calls[0] == (
        str(AUDIO),
        {
            "language": None,
            "vad_filter": True,
            # Not the library's 400 ms. At that padding the filter clips the
            # onset of a word off a real recording -- VAD_SPEECH_PAD_MS has the
            # measurement, and it is the difference between "place an order" and
            # "blaze an order".
            "vad_parameters": {"speech_pad_ms": VAD_SPEECH_PAD_MS},
        },
    )


def test_a_configured_language_is_passed_through_and_absence_means_detect():
    # None rather than a default of "en", which is what keeps the Russian fixture
    # transcribing as Russian on a stack nobody configured. memo_ai/config.py has
    # what pinning it buys and why it is still not the default.
    detecting = StubModel()
    provider(detecting).transcribe(AUDIO)

    assert detecting.calls[0][1]["language"] is None

    pinned = StubModel()
    provider(pinned, language="en").transcribe(AUDIO)

    assert pinned.calls[0][1]["language"] == "en"


def test_the_model_is_loaded_once_and_kept():
    loads = []

    def loader(size):
        loads.append(size)

        return StubModel()

    local = LocalWhisperStt("small", loader=loader)
    local.transcribe(AUDIO)
    local.transcribe(AUDIO)

    assert loads == ["small"]


def test_constructing_a_provider_loads_nothing():
    # The property that keeps `docker compose up` converging: resolving a provider
    # happens at boot on both replicas, and a bad STT_MODEL must not be able to
    # turn `restart: unless-stopped` into a restart loop.
    loads = []
    LocalWhisperStt("base", loader=lambda size: loads.append(size) or StubModel())

    assert loads == []


def test_prefetch_starts_the_load_without_waiting_for_a_memo():
    # What stops a 1.6 GB default from landing on somebody's first recording: the
    # download is spent against the idle minutes after `docker compose up`.
    release = threading.Event()
    loads = []

    def loader(size):
        loads.append(size)
        release.wait(5)

        return StubModel(texts=("warmed",))

    local = LocalWhisperStt("large-v3-turbo", loader=loader)
    local.prefetch()

    # Returned immediately, with the load still running behind it. Nothing raised
    # and nothing waited on -- a worker must reach its claim loop either way.
    assert loads == ["large-v3-turbo"]

    release.set()

    assert local.transcribe(AUDIO).text == "warmed"
    assert loads == ["large-v3-turbo"]


def test_prefetch_is_idempotent_and_does_not_disturb_a_loaded_model():
    loads = []
    local = LocalWhisperStt("base", loader=lambda size: loads.append(size) or StubModel())

    local.prefetch()
    local.prefetch()
    local.transcribe(AUDIO)
    local.prefetch()

    assert loads == ["base"]


def test_a_prefetch_that_fails_does_not_raise_and_leaves_the_memo_to_report_it():
    # A failed download is not a reason to stop serving text memos, so this is
    # silent at boot. The first voice memo finds the failure and the ordinary
    # retry path takes over from there.
    def loader(size):
        raise RuntimeError("cold cache, no network")

    local = LocalWhisperStt("base", loader=loader)
    local.prefetch()

    with pytest.raises(SttUnavailable, match="could not be loaded"):
        local.transcribe(AUDIO)


def test_a_failed_load_is_unavailable_and_is_retried_on_the_next_memo():
    attempts = []

    def loader(size):
        attempts.append(size)

        if len(attempts) == 1:
            raise ValueError("Invalid model size 'base'")

        return StubModel(texts=("second time", ""))

    local = LocalWhisperStt("base", loader=loader)

    with pytest.raises(SttUnavailable) as raised:
        local.transcribe(AUDIO)

    # Unavailable rather than terminal, so the chain walks to the fallback -- and
    # the message names the variable to look at rather than quoting the library.
    assert "STT_MODEL" in str(raised.value)
    assert "ValueError" not in str(raised.value)

    # Not cached as broken. A load failure is as likely to be a transient fetch as
    # a permanent misconfiguration, and caching it would make the first kind
    # permanent for the life of the container.
    assert local.transcribe(AUDIO).text == "second time"
    assert attempts == ["base", "base"]


def test_a_slow_load_gives_up_without_abandoning_the_download(monkeypatch):
    monkeypatch.setattr("memo_ai.stt.local.MODEL_LOAD_TIMEOUT_SECONDS", 0.05)

    release = threading.Event()
    finished = threading.Event()

    def loader(size):
        release.wait(5)
        finished.set()

        return StubModel(texts=("arrived late",))

    local = LocalWhisperStt("base", loader=loader)

    with pytest.raises(SttUnavailable, match="still being downloaded"):
        local.transcribe(AUDIO)

    # The load was left running rather than cancelled, which is the whole design:
    # this memo fails fast and the next one finds the model warm.
    release.set()
    assert finished.wait(5)
    assert local.transcribe(AUDIO).text == "arrived late"


def test_a_second_memo_during_a_slow_load_does_not_start_a_second_one(monkeypatch):
    monkeypatch.setattr("memo_ai.stt.local.MODEL_LOAD_TIMEOUT_SECONDS", 0.05)

    release = threading.Event()
    starts = []

    def loader(size):
        starts.append(size)
        release.wait(5)

        return StubModel()

    local = LocalWhisperStt("base", loader=loader)

    for _ in range(3):
        with pytest.raises(SttUnavailable):
            local.transcribe(AUDIO)

    release.set()

    assert starts == ["base"]


def test_callers_waiting_on_one_failed_load_start_one_retry_between_them():
    # Three callers, one load, one retry -- not three of each. On a cold cache
    # every extra load is another 142 MB and another copy of the model in memory,
    # and the shape that produces them is a waiter that retires a load it no
    # longer owns. Nothing here writes to that handle but the one block that
    # decides, which is what makes the count below hold.
    #
    # Timing-shaped, unavoidably: the barrier gets all three callers to the same
    # instruction and the sleep covers the few after it, before the load is
    # allowed to fail. Generous by two orders of magnitude against what those
    # instructions take.
    release = threading.Event()
    gathered = threading.Barrier(3)
    starts = []
    errors = []

    def loader(size):
        starts.append(size)
        release.wait(5)

        raise RuntimeError("cold cache, no network")

    local = LocalWhisperStt("base", loader=loader)

    def call():
        gathered.wait(5)

        try:
            local.transcribe(AUDIO)
        except Exception as error:  # noqa: BLE001 -- collected and asserted below
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(3)]

    for thread in threads:
        thread.start()

    time.sleep(0.2)
    release.set()

    for thread in threads:
        thread.join(5)

    assert starts == ["base"]
    assert len(errors) == 3
    assert all(isinstance(error, SttUnavailable) for error in errors)

    # And the failure is not cached: the next memo gets exactly one fresh attempt.
    # `release` is still set, so this one fails immediately rather than blocking.
    with pytest.raises(SttUnavailable):
        local.transcribe(AUDIO)

    assert starts == ["base", "base"]


def test_a_runaway_decode_is_stopped_by_the_deadline(monkeypatch):
    # A hundredth of a second of audio, so four times it is under the floor and the
    # floor is the deadline: 0.05s, against segments that take 0.04s each. The
    # second one is what trips it, which is also the property being checked --
    # the generator is drained lazily, so the stop happens partway through rather
    # than after the whole thing has already run.
    monkeypatch.setattr("memo_ai.stt.local.DEADLINE_FLOOR_SECONDS", 0.05)

    with pytest.raises(SttError) as raised:
        provider(texts=("a", "b", "c", "d"), duration=0.01, pause=0.04).transcribe(AUDIO)

    assert "took too long" in str(raised.value)
    # Terminal, not unavailable. The deadline has already spent the job's time
    # budget, and walking to a fallback would spend it again.
    assert not isinstance(raised.value, SttUnavailable)


def test_the_deadline_scales_with_the_audio_and_has_a_floor():
    assert _deadline_seconds(5.0) == DEADLINE_FLOOR_SECONDS
    assert _deadline_seconds(600.0) == 2400.0


def test_silence_fails_with_a_sentence_rather_than_an_empty_transcript():
    # An empty string in `transcript` would leave a `ready` memo with nothing in
    # it, nothing to search and nothing to enrich, and no explanation on the row.
    with pytest.raises(SttError, match="No speech"):
        provider(texts=("", "   ")).transcribe(AUDIO)


def test_an_empty_recording_is_told_apart_from_a_silent_one():
    with pytest.raises(SttError, match="empty"):
        provider(texts=(), duration=0.0).transcribe(AUDIO)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory"),
        ValueError("Invalid data found when processing input"),
    ],
)
def test_audio_the_decoder_cannot_read_is_terminal(error):
    # PyAV raises builtin FileNotFoundError and ValueError subclasses for these,
    # which is what lets this module classify them without importing `av`.
    with pytest.raises(SttError) as raised:
        provider(StubModel(raises=error)).transcribe(AUDIO)

    assert not isinstance(raised.value, SttUnavailable)
    # The message points at the normalized copy, not at the user's recording:
    # ffmpeg already read that one successfully.
    assert "prepared copy" in str(raised.value)


def test_running_out_of_memory_is_unavailable_rather_than_the_memos_fault():
    with pytest.raises(SttUnavailable, match="out of memory"):
        provider(StubModel(raises=MemoryError())).transcribe(AUDIO)


def test_an_engine_failure_is_unavailable_and_does_not_blame_the_recording():
    # CTranslate2 reports allocation failures as RuntimeError from C++ with
    # `bad_alloc` in the text, so those get the memory sentence and its knob.
    with pytest.raises(SttUnavailable, match="out of memory"):
        provider(StubModel(raises=RuntimeError("std::bad_alloc"))).transcribe(AUDIO)

    # Everything else the engine can fail at is still not the audio's fault. The
    # first version of this branch reused the unreadable-file sentence, which
    # would have sent somebody off to re-record over a server misconfiguration.
    with pytest.raises(SttUnavailable) as raised:
        provider(StubModel(raises=RuntimeError("unsupported compute type"))).transcribe(AUDIO)

    message = str(raised.value)

    assert "out of memory" not in message
    assert "nothing is wrong with the recording" in message


def test_no_library_message_reaches_the_row():
    # `last_error` is part of the API's response projection, so every string a
    # provider raises renders in the browser. Nothing a dependency wrote gets
    # there -- the detail goes to the log instead.
    secrets = "libavformat error at /data/audio/2026/07/31/memo.webm"

    with pytest.raises(SttError) as raised:
        provider(StubModel(raises=ValueError(secrets))).transcribe(AUDIO)

    assert "/data/audio" not in str(raised.value)

"""
Normalization and duration, against a real ffmpeg.

These are the tests that cannot be written with doubles, because what is being
checked *is* ffmpeg's behaviour: that it recovers a duration the source does not
carry, and that the file it writes is the one a provider was promised. Everything
in memo_ai/audio.py that is a decision rather than a transcode is checked in
tests/test_pipeline.py instead, with normalization stubbed out.

Skipped rather than failed when ffmpeg is absent. The suite is run from the image
built by ai/Dockerfile, which has it (README.md's Development section has the
invocation); a developer running pytest on a bare host without it should see the
rest of the suite pass and one clear skip, not a wall of red about a binary the
project never asked them to install.

The three-browser acceptance criterion lives in tests/test_fixtures.py, which
needs real recordings rather than anything synthesizable here.
"""

import subprocess
from pathlib import Path

import pytest

from memo_ai import audio
from memo_ai.stt.base import SttUnavailable

pytestmark = pytest.mark.skipif(
    not audio.ffmpeg_available(),
    reason="ffmpeg and ffprobe are not on PATH; run this suite from the ai image",
)

# Long enough that the 20 ms Opus frame padding is a rounding detail rather than a
# large fraction of the measurement, short enough that the suite stays quick.
CLIP_SECONDS = 3.4


def sine(destination: Path, seconds: float = CLIP_SECONDS) -> Path:
    """A synthesized WebM/Opus clip, written to a real path -- so it is seekable."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", "libopus", "-b:a", "48k",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )

    return destination


def sine_without_duration(destination: Path, seconds: float = CLIP_SECONDS) -> Path:
    """
    The same clip, written through a pipe.

    That is what reproduces the defect this module exists for. MediaRecorder
    streams its container to a sink it cannot seek back into, so it never returns
    to fill in the Duration element -- and ffmpeg writing to stdout is in exactly
    that position. Not a substitute for a real browser recording (see
    tests/test_fixtures.py), but it is the same missing field.
    """
    completed = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", "libopus", "-b:a", "48k",
            "-f", "webm", "-",
        ],
        check=True,
        capture_output=True,
    )
    destination.write_bytes(completed.stdout)

    return destination


def probe(path: Path, entries: str) -> str:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", entries,
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return completed.stdout.strip()


def test_the_defect_this_module_exists_for_is_real(tmp_path):
    # The premise, asserted rather than assumed. If ffprobe ever starts answering
    # with a duration here, the reason for normalizing before measuring is gone
    # and this whole module is worth revisiting -- so it should fail loudly.
    assert probe(sine(tmp_path / "seekable.webm"), "format=duration") != "N/A"
    assert probe(sine_without_duration(tmp_path / "piped.webm"), "format=duration") == "N/A"


def test_a_duration_is_recovered_from_a_source_that_reports_none(tmp_path):
    source = sine_without_duration(tmp_path / "piped.webm")

    with audio.normalize(source, audio.OPUS, max_seconds=60) as normalized:
        # Within one Opus frame of the truth. The encoder pads the final frame, so
        # the measurement rounds up by up to 20 ms and never down.
        assert CLIP_SECONDS * 1000 <= normalized.duration_ms <= CLIP_SECONDS * 1000 + 20


def test_wav_is_the_format_that_actually_reports_16_kHz(tmp_path):
    # The acceptance criterion says "16 kHz mono", and for WAV ffprobe says so
    # directly.
    source = sine_without_duration(tmp_path / "piped.webm")

    with audio.normalize(source, audio.WAV, max_seconds=60) as normalized:
        assert probe(normalized.path, "stream=sample_rate") == "16000"
        assert probe(normalized.path, "stream=channels") == "1"


def test_opus_is_mono_and_encoded_from_16_kHz_but_reports_48000(tmp_path):
    # The gotcha, pinned here so nobody spends an afternoon on it. Opus *always*
    # decodes at 48 kHz -- the rate is fixed by the format, and `-ar 16000` sets
    # what the encoder is fed, not what the stream advertises. So the bandwidth
    # reduction is real (everything above 8 kHz is gone, and the file is a
    # fraction of the size) while ffprobe reports 48000 either way.
    #
    # An acceptance check written as `sample_rate == 16000` therefore fails on the
    # default format while the pipeline is entirely correct.
    source = sine_without_duration(tmp_path / "piped.webm")

    with audio.normalize(source, audio.OPUS, max_seconds=60) as normalized:
        assert probe(normalized.path, "stream=channels") == "1"
        assert probe(normalized.path, "stream=codec_name") == "opus"
        assert probe(normalized.path, "stream=sample_rate") == "48000"


def test_opus_is_far_smaller_than_wav_which_is_why_it_is_the_default(tmp_path):
    # The reason DEFAULT_FORMAT is not WAV: 16 kHz mono WAV is larger than the
    # browser's own recording and would eat most of a hosted API's request limit.
    source = sine_without_duration(tmp_path / "piped.webm")

    with audio.normalize(source, audio.OPUS, max_seconds=60) as as_opus:
        opus_bytes = as_opus.path.stat().st_size

    with audio.normalize(source, audio.WAV, max_seconds=60) as as_wav:
        wav_bytes = as_wav.path.stat().st_size

    assert wav_bytes > opus_bytes * 4


def test_audio_over_the_cap_is_refused_and_carries_its_own_duration(tmp_path):
    source = sine_without_duration(tmp_path / "piped.webm")

    with pytest.raises(audio.AudioTooLong) as raised:
        with audio.normalize(source, audio.OPUS, max_seconds=1.0):
            pytest.fail("normalize yielded audio that is over the cap")

    assert raised.value.duration_ms >= CLIP_SECONDS * 1000


def test_the_refusal_reads_as_a_sentence_with_both_lengths_in_it(tmp_path):
    # `last_error` is rendered verbatim in the browser, so the wording is part of
    # the contract and not just a log line. `m:ss` matches the timer the recorder
    # showed while this memo was being recorded.
    #
    # Both numbers round up, which is what keeps the sentence from contradicting
    # itself: a 3.4 second clip reads 0:04, not the 0:03 the recorder's own
    # (deliberately flooring) timer would have shown.
    source = sine_without_duration(tmp_path / "piped.webm")

    with pytest.raises(audio.AudioTooLong) as raised:
        with audio.normalize(source, audio.OPUS, max_seconds=1.0):
            pytest.fail("normalize yielded audio that is over the cap")

    assert str(raised.value) == (
        "This recording is 0:04 long, which is over the 0:01 limit. Record a shorter memo."
    )


def test_the_normalized_file_is_deleted_when_the_block_exits(tmp_path):
    # It is a derivative in /tmp, not something to leave behind on a worker that
    # runs for weeks. The original is a separate file and is never touched.
    source = sine_without_duration(tmp_path / "piped.webm")

    with audio.normalize(source, audio.OPUS, max_seconds=60) as normalized:
        written = normalized.path

        assert written.is_file()

    assert not written.exists()
    assert source.is_file()


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("empty.webm", b""),
        ("junk.webm", b"not audio at all, just some text pretending to be a container"),
    ],
)
def test_a_file_ffmpeg_cannot_decode_fails_without_leaking_its_stderr(tmp_path, name, contents):
    # ffmpeg writes things like `[matroska,webm @ 0xaaaadff92d60] EBML header
    # parsing failed` next to the full container path of the file. This message
    # goes to `memos.last_error`, which the API returns and the browser renders.
    source = tmp_path / name
    source.write_bytes(contents)

    with pytest.raises(audio.AudioError) as raised:
        with audio.normalize(source, audio.OPUS, max_seconds=60):
            pytest.fail("normalize yielded audio from a file ffmpeg cannot read")

    message = str(raised.value)

    assert message == (
        "This recording could not be decoded. It may be incomplete or in a format "
        "this server does not support."
    )
    assert str(tmp_path) not in message
    assert "0x" not in message


def test_a_video_with_no_audio_track_says_so_rather_than_claiming_it_is_corrupt(tmp_path):
    # SniffedAudioType accepts real video files and points here as the thing that
    # "takes the audio stream and discards the rest" -- so this is a reachable
    # upload, not a hypothetical. Left to ffmpeg it exits 234 with "Output file
    # does not contain any stream", which would have put "could not be decoded" on
    # a file that decoded perfectly.
    source = tmp_path / "screencap.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-c:v", "libx264", str(source),
        ],
        check=True,
        capture_output=True,
    )

    with pytest.raises(audio.AudioError) as raised:
        with audio.normalize(source, audio.OPUS, max_seconds=60):
            pytest.fail("normalize yielded audio from a file that has none")

    assert str(raised.value) == "This file has no audio track, so there is nothing to transcribe."


def test_a_missing_binary_is_reported_as_a_configuration_problem(tmp_path, monkeypatch):
    # A real source, so the audio-stream pre-check passes and the failure is the
    # one being tested rather than an undecodable file.
    #
    # Not SttUnavailable, deliberately: that subclass means "walk the fallback
    # chain", and every provider in that chain is fed by this module, so walking
    # it would just find the same missing binary.
    source = sine_without_duration(tmp_path / "piped.webm")
    monkeypatch.setattr(audio, "_ffmpeg_command", lambda *_: ["memo-no-such-binary"])

    with pytest.raises(audio.AudioError) as raised:
        with audio.normalize(source, audio.OPUS, max_seconds=60):
            pytest.fail("normalize yielded audio without ffmpeg present")

    assert not isinstance(raised.value, SttUnavailable)
    assert "not installed in this image" in str(raised.value)

"""
MEMO-13's acceptance criterion, against real browser recordings.

    Feed one recording from each of the three browsers through normalization.
    All three produce a playable 16 kHz mono file and a correct duration.

Three containers, one decode path: Chrome writes WebM/Opus, Firefox writes
Ogg/Opus (while reporting `audio/webm` -- Mozilla bug 1501308), and Safari writes
MP4/AAC. ``App\\Http\\Rules\\SniffedAudioType`` has the full table and the reason
two of the three sniff as ``video/``.

**Why these files cannot be synthesized.** MediaRecorder streams its container to
a sink it cannot seek back into, so the duration is never written --
``tests/fixtures/README.md`` has the detail and the capture instructions.
tests/test_audio.py reproduces the missing field with a pipe, which covers the
mechanics; what only a real recording covers is each browser's own quirks.

Skipped, not failed, when a fixture is absent, so that a stranger cloning the repo
gets a green suite. Skipping is per browser, so whichever recordings exist are
actually asserted against and the skip names only what is still missing.
"""

import subprocess
from pathlib import Path

import pytest

from memo_ai import audio

FIXTURES = Path(__file__).parent / "fixtures"

# The stem is what identifies a fixture; the extension is whatever the browser's
# download was called. Firefox in particular hands back a file named `.webm`
# containing Ogg, and that mislabelling is itself part of what is being tested.
BROWSERS = ("chrome", "firefox", "safari")

# Deliberately loose. A recording made by hand is a few seconds; the assertion is
# that the duration is a real measurement of a real clip, not that it is any
# particular length. Nothing below hard-codes a number a future recapture would
# have to match.
MIN_MS = 300
MAX_MS = 120_000


def recording(browser: str) -> Path | None:
    found = sorted(p for p in FIXTURES.glob(f"{browser}.*") if p.suffix != ".md")

    return found[0] if found else None


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


@pytest.fixture(scope="module", autouse=True)
def require_ffmpeg():
    if not audio.ffmpeg_available():
        pytest.skip("ffmpeg and ffprobe are not on PATH; run this suite from the ai image")


def fixture_for(browser: str) -> Path:
    """
    The recording for one browser, or skip this test alone.

    Per-browser rather than all-or-nothing, which is what this was first. These
    files arrive one at a time -- whoever is capturing them has to open three
    different browsers, and Safari needs a secure context on top of that -- so a
    guard that skipped the whole module until all three landed would report the
    same nine skips whether two were present or none. Coverage that exists should
    count, and coverage that does not should say which.
    """
    found = recording(browser)

    if found is None:
        pytest.skip(
            f"no real recording for {browser}. MEMO-13's acceptance needs genuine "
            f"MediaRecorder output -- see ai/tests/fixtures/README.md for how to capture it."
        )

    return found


@pytest.mark.parametrize("browser", BROWSERS)
def test_a_real_recording_normalizes_to_mono_with_a_usable_duration(browser):
    source = fixture_for(browser)

    with audio.normalize(source, audio.OPUS, max_seconds=600) as normalized:
        assert normalized.path.stat().st_size > 0
        assert probe(normalized.path, "stream=channels") == "1"
        assert probe(normalized.path, "stream=codec_name") == "opus"

        # "Playable" means a decoder can read it back, which is a stronger claim
        # than "ffmpeg exited 0" and is the one the criterion makes. Decoding to
        # null keeps it cheap and still fails on a truncated or corrupt file.
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(normalized.path), "-f", "null", "-"],
            check=True,
            capture_output=True,
        )

        assert MIN_MS <= normalized.duration_ms <= MAX_MS


@pytest.mark.parametrize("browser", BROWSERS)
def test_the_wav_path_reports_16_khz_exactly(browser):
    # The criterion says "16 kHz mono". WAV is where ffprobe says so directly --
    # an Opus stream always advertises 48000 no matter what it was encoded from,
    # which tests/test_audio.py pins separately. Same transcode, same duration,
    # so this also checks the two formats agree about the length.
    source = fixture_for(browser)

    with audio.normalize(source, audio.WAV, max_seconds=600) as as_wav:
        assert probe(as_wav.path, "stream=sample_rate") == "16000"
        assert probe(as_wav.path, "stream=channels") == "1"

        with audio.normalize(source, audio.OPUS, max_seconds=600) as as_opus:
            # Within one Opus frame. WAV is sample-exact; Opus pads its last frame.
            assert abs(as_opus.duration_ms - as_wav.duration_ms) <= 20


@pytest.mark.parametrize("browser", BROWSERS)
def test_the_duration_survives_a_source_that_does_not_carry_one(browser):
    # The defect in one assertion. Whether a given browser's container happens to
    # carry a duration is not something this project controls -- Safari's MP4
    # often does, Chrome's WebM does not -- so this asserts the useful half: after
    # normalization there is always a number, whatever the source said.
    source = fixture_for(browser)

    with audio.normalize(source, audio.OPUS, max_seconds=600) as normalized:
        assert normalized.duration_ms > 0

"""
MEMO-14's acceptance criterion, run against the real model and real recordings.

Everything else about the local provider is checked through a stub, because what
those tests are about is classification. This file is about the one claim a stub
cannot make: that the same audio goes through both providers, that `fake` is
instant and `local` produces words, and that `local` needs no key and no network.

It skips rather than fails when the model is not on this machine. Three reasons a
run legitimately has no model -- a clean clone, an image without faster-whisper in
it, or a `whisper-cache` volume that has never been filled -- and none of them
should turn a green suite red for whoever cloned the repo. What it must not do is
skip *silently* on a machine that does have one, which is why the guard probes the
HuggingFace cache directly rather than trying a load and swallowing the error.

Once MEMO-15 bakes the weights into ai/Dockerfile the guard is satisfied by the
image itself and this file always runs.
"""

import re
import time
from pathlib import Path

import pytest

from memo_ai import audio, stt
from memo_ai.config import DEFAULT_STT_MODEL, Settings
from memo_ai.stt.base import SttError
from memo_ai.stt.fake import CANNED_TRANSCRIPT

FIXTURES = Path(__file__).parent / "fixtures"

# Read from the config rather than named here, so this always exercises whatever
# is actually shipped. A faster test against `tiny` would prove nothing about the
# configuration a stranger gets, and a hardcoded `base` silently stopped being
# that configuration the moment the default changed.
MODEL = DEFAULT_STT_MODEL

SETTINGS = Settings.from_env(
    {"DATABASE_URL": "postgresql://memo:memo@db:5432/memo", "STT_MODEL": MODEL}
)


def _model_is_cached() -> bool:
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return False

    try:
        # local_files_only, so the guard itself can never trigger the download it
        # is checking for -- a test suite that quietly pulled 145 MB would be a
        # worse surprise than a skip.
        download_model(MODEL, local_files_only=True)
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not (audio.ffmpeg_available() and _model_is_cached()),
    reason=f"needs ffmpeg and a cached faster-whisper {MODEL!r} model",
)


def recordings():
    """The real browser recordings, or nothing. tests/fixtures/README.md has both."""
    return sorted(p for p in FIXTURES.glob("*.*") if p.suffix != ".md")


@pytest.fixture(scope="module")
def local():
    """One provider for the module, so the model is loaded once rather than per test."""
    return stt.resolve("local", SETTINGS)


@pytest.mark.parametrize("name", ["chrome", "firefox", "safari"])
def test_a_real_recording_transcribes_on_the_local_model(local, name):
    matches = [p for p in recordings() if p.stem == name]

    if not matches:
        pytest.skip(f"no {name} recording in tests/fixtures/")

    with audio.normalize(matches[0], audio.format_for(local), 600.0) as prepared:
        result = local.transcribe(prepared.path)

    # All three fixtures are somebody counting to ten -- in English for Chrome and
    # Safari, in Russian for Firefox -- so the assertion that holds across all
    # three is that something came back, not what.
    #
    # Not an assertion on the words, and the Russian recording is why. At the
    # library's default beam size all three transcribe as the digits `1,2,3...`
    # rather than as words, in either language, so a fixture-by-fixture expected
    # string would be asserting on whisper's number formatting. What the Russian
    # one does prove is in the worker log rather than here: language `ru` at 0.90,
    # detected rather than configured.
    assert result.text.strip()
    assert result.provider == "local"
    assert result.model == MODEL


def test_the_same_audio_goes_through_both_providers(local):
    """The acceptance sentence, verbatim: the same audio through both providers."""
    fixtures = recordings()

    if not fixtures:
        pytest.skip("no recordings in tests/fixtures/")

    source = fixtures[0]
    fake = stt.resolve("fake", SETTINGS)

    with audio.normalize(source, audio.format_for(fake), 600.0) as prepared:
        started = time.monotonic()
        canned = fake.transcribe(prepared.path)
        fake_seconds = time.monotonic() - started

    with audio.normalize(source, audio.format_for(local), 600.0) as prepared:
        real = local.transcribe(prepared.path)

    assert canned.text == CANNED_TRANSCRIPT
    assert real.text.strip() and real.text != CANNED_TRANSCRIPT

    # "`fake` is instant" as a number rather than a claim. Generous by three
    # orders of magnitude against the microseconds it actually takes, because
    # what would break this assertion is the fake starting to open the file, not
    # a slow machine.
    assert fake_seconds < 0.1

    # And the two are told apart on the row, which is what makes the seam worth
    # having: `memos.stt_provider` records which one ran.
    assert (canned.provider, canned.model) == ("fake", None)
    assert (real.provider, real.model) == ("local", MODEL)


def test_the_two_normalized_formats_reach_the_same_words(local):
    # The chain hands a fallback whatever the *primary* asked for, so a `local`
    # behind an Opus-wanting primary is fed Opus. That is only safe if the two
    # formats transcribe the same, which is checked here rather than asserted in
    # the comment that relies on it.
    #
    # The same *words*, not the same string, and the difference was found rather
    # than anticipated. On `base` the two outputs were byte-identical; on the
    # `large-v3-turbo` this project now ships they differ by a trailing full stop
    # on the Opus side. So the codec does reach the output, just not the content
    # -- and the claim worth pinning is the one the fallback depends on.
    fixtures = recordings()

    if not fixtures:
        pytest.skip("no recordings in tests/fixtures/")

    transcripts = []

    for fmt in (audio.WAV, audio.OPUS):
        with audio.normalize(fixtures[0], fmt, 600.0) as prepared:
            transcripts.append(local.transcribe(prepared.path).text)

    assert _words(transcripts[0]) == _words(transcripts[1])


def _words(text: str) -> list[str]:
    """Lowercased word tokens, so punctuation and spacing drop out of a comparison."""
    return re.findall(r"\w+", text.lower())


def test_silence_fails_with_a_readable_reason_rather_than_an_empty_memo(local, tmp_path):
    import subprocess

    silence = tmp_path / "silence.wav"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-t", "3", "-c:a", "pcm_s16le", str(silence),
        ],
        check=True,
    )

    with pytest.raises(SttError, match="No speech"):
        local.transcribe(silence)


def test_a_corrupt_file_fails_cleanly_instead_of_hanging(local, tmp_path):
    # The other half of the acceptance criterion. This one bypasses normalization
    # on purpose: ffmpeg would refuse these bytes first (memo_ai/audio.py covers
    # that path), and what is being checked here is that the provider itself does
    # not hang or leak a library message when handed something unreadable.
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\xff" * 512)

    started = time.monotonic()

    with pytest.raises(SttError) as raised:
        local.transcribe(corrupt)

    assert time.monotonic() - started < 10
    assert str(tmp_path) not in str(raised.value)

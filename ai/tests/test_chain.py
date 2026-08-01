"""When the fallback provider is walked to, and when it is deliberately not."""

from pathlib import Path

import pytest

from memo_ai import audio, stt
from memo_ai.stt.base import SttError, SttUnavailable, Transcript
from memo_ai.stt.chain import FallbackStt
from memo_ai.stt.fake import FakeStt
from memo_ai.stt.local import LocalWhisperStt

AUDIO = Path("/tmp/normalized.opus")


class Stub:
    """A provider in five lines, importing nothing but Transcript — see SttProvider."""

    def __init__(self, name, error=None, audio_format=None):
        self.name = name
        self.calls = 0
        self._error = error

        if audio_format is not None:
            self.audio_format = audio_format

    def transcribe(self, source: Path) -> Transcript:
        self.calls += 1

        if self._error is not None:
            raise self._error

        return Transcript(text=f"from {self.name}", provider=self.name, model="stub-1")


def settings(provider: str, fallback: str) -> object:
    from memo_ai.config import Settings

    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://memo:memo@db:5432/memo",
            "STT_PROVIDER": provider,
            "STT_FALLBACK": fallback,
        }
    )


def test_the_primary_answers_and_the_fallback_is_never_called():
    primary, fallback = Stub("primary"), Stub("fallback")

    result = FallbackStt(primary, fallback).transcribe(AUDIO)

    assert result.text == "from primary"
    assert fallback.calls == 0


def test_an_unavailable_primary_walks_to_the_fallback():
    primary = Stub("primary", error=SttUnavailable("no model here"))
    fallback = Stub("fallback")

    result = FallbackStt(primary, fallback).transcribe(AUDIO)

    # The row records what actually produced the words, not what was configured.
    assert result.text == "from fallback"
    assert result.provider == "fallback"


def test_a_terminal_error_is_not_retried_on_the_fallback():
    # Both providers are handed the same normalized file, so a recording that
    # yielded no transcript on one will yield none on the other. Walking the chain
    # would spend a second provider's time to reach the same answer -- and on a
    # hosted one, spend money.
    primary = Stub("primary", error=SttError("no speech in this recording"))
    fallback = Stub("fallback")

    with pytest.raises(SttError, match="no speech"):
        FallbackStt(primary, fallback).transcribe(AUDIO)

    assert fallback.calls == 0


def test_an_unclassified_exception_is_not_caught_here():
    # A bug, not an outcome. memo_ai/pipeline.py logs the traceback and writes a
    # generic sentence; falling back would turn it into a slower success that
    # nobody ever investigates.
    primary = Stub("primary", error=KeyError("typo"))
    fallback = Stub("fallback")

    with pytest.raises(KeyError):
        FallbackStt(primary, fallback).transcribe(AUDIO)

    assert fallback.calls == 0


def test_the_fallbacks_own_error_is_what_reaches_the_row():
    primary = Stub("primary", error=SttUnavailable("primary is down"))
    fallback = Stub("fallback", error=SttError("this recording is silent"))

    with pytest.raises(SttError) as raised:
        FallbackStt(primary, fallback).transcribe(AUDIO)

    # The fallback is the attempt that actually read the audio, so its sentence is
    # the one about this memo. The primary's goes to the log.
    assert str(raised.value) == "this recording is silent"


def test_the_chain_asks_for_the_primarys_audio_format():
    wants_wav = Stub("primary", audio_format=audio.WAV)

    assert audio.format_for(FallbackStt(wants_wav, Stub("fallback"))) is audio.WAV


def test_a_chain_of_two_indifferent_providers_still_gets_the_default_format():
    # Neither stub declares one, so `format_for` must reach its own default rather
    # than trip over the None this class stores.
    assert audio.format_for(FallbackStt(Stub("a"), Stub("b"))) is audio.DEFAULT_FORMAT


def test_the_shipped_default_resolves_to_one_provider_not_a_chain():
    # docker-compose.yml ships STT_PROVIDER=local and STT_FALLBACK=local. A chain
    # built from one provider twice would fail a stuck model load twice per memo,
    # each attempt waiting out its own load timeout.
    resolved = stt.resolve_chain(settings("local", "local"))

    assert isinstance(resolved, LocalWhisperStt)


def test_differing_names_resolve_to_a_chain_of_both():
    resolved = stt.resolve_chain(settings("fake", "local"))

    assert isinstance(resolved, FallbackStt)
    assert isinstance(resolved.primary, FakeStt)
    assert isinstance(resolved.fallback, LocalWhisperStt)


def test_the_documented_openai_setting_falls_through_rather_than_dead_ending():
    # `openai` is recognised and not built. It is still not a dead end, because
    # UnimplementedStt raises SttUnavailable: the memo transcribes on whatever
    # STT_FALLBACK names -- `local` as shipped, `fake` here so the test loads no
    # model -- and `memos.stt_provider` records that one rather than the name that
    # was asked for.
    resolved = stt.resolve_chain(settings("openai", "fake"))

    assert resolved.transcribe(AUDIO).provider == "fake"


def test_a_typo_in_the_fallback_is_still_a_boot_failure():
    from memo_ai.config import ConfigError

    with pytest.raises(ConfigError, match="STT_FALLBACK"):
        stt.resolve_chain(settings("local", "fak"))

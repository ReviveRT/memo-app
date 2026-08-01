"""The provider seam: what resolves, what runs, and what refuses."""

from pathlib import Path

import pytest

from memo_ai import stt
from memo_ai.config import ConfigError, Settings
from memo_ai.stt.fake import CANNED_TRANSCRIPT, FakeStt
from memo_ai.stt.local import LocalWhisperStt
from memo_ai.stt.unimplemented import UnimplementedStt

SETTINGS = Settings.from_env({"DATABASE_URL": "postgresql://memo:memo@db:5432/memo"})


def test_fake_resolves_and_returns_the_canned_transcript():
    provider = stt.resolve("fake", SETTINGS)

    result = provider.transcribe(Path("/data/audio/anything.webm"))

    assert isinstance(provider, FakeStt)
    assert result.text == CANNED_TRANSCRIPT
    assert result.provider == "fake"


def test_fake_records_no_model():
    # `memos.stt_model` stays NULL rather than claiming STT_MODEL produced this.
    # That column is what MEMO-22 prices a run from, so a canned transcript
    # attributed to `base` would be a number nobody can reconcile.
    assert stt.resolve("fake", SETTINGS).transcribe(Path("/x.webm")).model is None


def test_fake_never_touches_the_filesystem():
    # The path below does not exist, and it must not matter. This is what lets
    # MEMO-08 (build order 9) prove the queue before MEMO-11 (build order 14) can
    # put real bytes on the volume, and what MEMO-14 means by "`fake` is instant".
    result = stt.resolve("fake", SETTINGS).transcribe(
        Path("/data/audio/definitely/not/here/nope.webm")
    )

    assert result.text == CANNED_TRANSCRIPT


def test_local_resolves_to_the_real_provider_and_carries_the_configured_model():
    # The one thing `settings` was in resolve's signature for since MEMO-08.
    provider = stt.resolve(
        "local",
        Settings.from_env(
            {"DATABASE_URL": "postgresql://memo:memo@db:5432/memo", "STT_MODEL": "small"}
        ),
    )

    assert isinstance(provider, LocalWhisperStt)
    assert provider.model_size == "small"
    # Resolve runs at boot on both replicas whether or not a voice memo ever
    # arrives, so it must not fetch a weight. tests/test_local_stt.py proves that
    # against a loader; this only checks the default is carried, not acted on.
    assert stt.resolve("local", SETTINGS).model_size == "base"


def test_a_declared_but_unbuilt_provider_resolves_and_fails_only_on_use():
    # The split that keeps `docker compose up` converging: a name in the README's
    # variable table must not be able to stop the worker starting. It is a
    # per-memo failure instead, and SttUnavailable means the chain routes around
    # it -- see tests/test_chain.py.
    provider = stt.resolve("openai", SETTINGS)

    assert isinstance(provider, UnimplementedStt)
    assert provider.name == "openai"

    with pytest.raises(stt.SttUnavailable) as raised:
        provider.transcribe(Path("/data/audio/memo.webm"))

    # The message is what lands in `memos.last_error` and reaches the browser, so
    # it has to name the way out rather than just the problem.
    assert "STT_PROVIDER=local" in str(raised.value)


def test_an_unbuilt_provider_is_not_silently_swapped_for_the_fake():
    # The worst available behaviour, stated as a test: substituting canned text for
    # a configuration that asked for real transcription would pass every acceptance
    # criterion and lie in production.
    assert stt.resolve("openai", SETTINGS).name == "openai"

    with pytest.raises(stt.SttUnavailable):
        stt.resolve("openai", SETTINGS).transcribe(Path("/x.webm"))


def test_an_unknown_provider_refuses_to_start_and_lists_the_valid_names():
    with pytest.raises(ConfigError) as raised:
        stt.resolve("whisper", SETTINGS)

    message = str(raised.value)

    assert "STT_PROVIDER" in message

    for name in stt.PROVIDER_NAMES:
        assert name in message


def test_the_fallback_name_is_validated_without_being_built():
    stt.require_known("STT_FALLBACK", "local")

    # A typo caught on the boot after the edit, rather than months later on the one
    # code path that only runs when something else has already gone wrong.
    with pytest.raises(ConfigError, match="STT_FALLBACK"):
        stt.require_known("STT_FALLBACK", "fak")


def test_the_registry_covers_every_name_the_configuration_surface_documents():
    # .env.example's STT_PROVIDER comment and the README's variable table both
    # offer these three. A name documented there and absent here would be rejected
    # at boot while the repo's own documentation recommended it.
    assert stt.PROVIDER_NAMES == frozenset({"fake", "local", "openai"})

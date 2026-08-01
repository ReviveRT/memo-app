"""
The two parsing rules that are shared with the API's ``App\\Support\\Env``, plus
the refusals.

Both rules were regressions on the PHP side rather than theory, which is why they
are pinned here on the first task that reads an environment variable in Python.
"""

import logging

import pytest

from memo_ai import stt
from memo_ai.config import (
    DEFAULT_AUDIO_DIR,
    DEFAULT_MAX_AUDIO_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_STT_FALLBACK,
    DEFAULT_STT_MODEL,
    DEFAULT_STT_PROVIDER,
    LOG_LEVELS,
    ConfigError,
    Settings,
)

MINIMAL = {"DATABASE_URL": "postgresql://memo:memo@db:5432/memo"}


def test_only_database_url_is_required():
    settings = Settings.from_env(MINIMAL)

    assert settings.database_url == MINIMAL["DATABASE_URL"]
    assert str(settings.audio_dir) == DEFAULT_AUDIO_DIR
    assert settings.stt_provider == DEFAULT_STT_PROVIDER
    assert settings.stt_fallback == DEFAULT_STT_FALLBACK
    assert settings.stt_model == DEFAULT_STT_MODEL
    assert settings.max_audio_seconds == DEFAULT_MAX_AUDIO_SECONDS
    assert settings.poll_seconds == DEFAULT_POLL_SECONDS


@pytest.mark.parametrize(
    ("key", "attribute", "expected"),
    [
        ("AUDIO_DIR", "audio_dir", DEFAULT_AUDIO_DIR),
        ("STT_PROVIDER", "stt_provider", DEFAULT_STT_PROVIDER),
        ("STT_FALLBACK", "stt_fallback", DEFAULT_STT_FALLBACK),
        ("STT_MODEL", "stt_model", DEFAULT_STT_MODEL),
        ("MAX_AUDIO_SECONDS", "max_audio_seconds", DEFAULT_MAX_AUDIO_SECONDS),
        ("WORKER_POLL_SECONDS", "poll_seconds", DEFAULT_POLL_SECONDS),
        ("LOG_LEVEL", "log_level", "INFO"),
    ],
)
def test_an_empty_string_is_an_absence_not_a_value(key, attribute, expected):
    # Rule 1, and the case that produces it in practice is not exotic: a
    # commented-out or blank line in someone's .env, or `docker run -e AUDIO_DIR=`.
    # `os.environ.get(key, default)` returns the '' because the key *is* present.
    #
    # AUDIO_DIR is the one with teeth. '' as the root would make every audio key
    # resolve against the filesystem root of the container -- the failure that put
    # App\Support\Env in the PHP side in the first place.
    settings = Settings.from_env(MINIMAL | {key: ""})

    assert str(getattr(settings, attribute)) == str(expected)


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_or_empty_database_url_refuses_to_start(value):
    env = dict(MINIMAL)

    if value is None:
        del env["DATABASE_URL"]
    else:
        env["DATABASE_URL"] = value

    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env(env)


@pytest.mark.parametrize("value", ["abc", "1s", ""])
def test_a_poll_interval_that_is_not_a_number_is_refused_or_defaulted(value):
    if value == "":
        assert Settings.from_env(MINIMAL | {"WORKER_POLL_SECONDS": value}).poll_seconds == (
            DEFAULT_POLL_SECONDS
        )

        return

    with pytest.raises(ConfigError, match="WORKER_POLL_SECONDS"):
        Settings.from_env(MINIMAL | {"WORKER_POLL_SECONDS": value})


@pytest.mark.parametrize("value", ["0", "-1", "-0.5"])
def test_a_non_positive_poll_interval_is_refused(value):
    # Rule 2 with the consequence that makes it worth refusing rather than
    # clamping: zero is not a fast poll, it is a loop with no sleep in it. Two
    # replicas would issue the claim statement as fast as Postgres could answer,
    # and that presents as a database problem rather than as a typo.
    with pytest.raises(ConfigError, match="greater than zero"):
        Settings.from_env(MINIMAL | {"WORKER_POLL_SECONDS": value})


def test_a_valid_poll_interval_is_parsed_as_a_float():
    assert Settings.from_env(MINIMAL | {"WORKER_POLL_SECONDS": "0.25"}).poll_seconds == 0.25


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_a_duration_cap_that_is_zero_negative_or_unparseable_is_refused(value):
    # Same rule as the poll interval and the same reason to refuse rather than
    # clamp, with a different consequence: MAX_AUDIO_SECONDS=0 is not a strict cap,
    # it is a configuration under which every voice memo fails on length and the
    # only clue is a `last_error` reading "over the 0:00 limit".
    with pytest.raises(ConfigError, match="MAX_AUDIO_SECONDS"):
        Settings.from_env(MINIMAL | {"MAX_AUDIO_SECONDS": value})


def test_a_valid_duration_cap_is_parsed_as_a_float():
    # Fractional values are legal, and the tests use them: a cap in whole seconds
    # would make every "too long" case a real multi-second recording.
    assert Settings.from_env(MINIMAL | {"MAX_AUDIO_SECONDS": "600"}).max_audio_seconds == 600.0
    assert Settings.from_env(MINIMAL | {"MAX_AUDIO_SECONDS": "1.5"}).max_audio_seconds == 1.5


def test_log_level_is_case_insensitive():
    assert Settings.from_env(MINIMAL | {"LOG_LEVEL": "debug"}).log_level == "DEBUG"


def test_every_offered_log_level_is_one_logging_can_resolve():
    # log.configure() indexes logging.getLevelNamesMapping() with whatever this
    # module accepted, so a name here that logging does not know would be a KeyError
    # at startup -- *after* config parsing had already pronounced it valid. That is
    # the worst place for it, because the error would name logging rather than the
    # variable the user set.
    for name in LOG_LEVELS:
        assert name in logging.getLevelNamesMapping()
        assert Settings.from_env(MINIMAL | {"LOG_LEVEL": name}).log_level == name


def test_an_unknown_log_level_is_refused_rather_than_defaulted():
    # getLevelName() answers the *string* "Level INF" for an unknown name instead
    # of raising, so a typo here would otherwise survive as far as a logger that
    # emits nothing.
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(MINIMAL | {"LOG_LEVEL": "INF"})


def test_the_default_provider_is_one_the_registry_knows():
    # The invariant that keeps `docker compose up` working on a clean checkout. If
    # the default in config.py ever names a provider stt.resolve() does not
    # recognise, every boot fails at once with "unknown provider" -- while
    # .env.example and the README go on recommending it.
    assert DEFAULT_STT_PROVIDER in stt.PROVIDER_NAMES
    assert DEFAULT_STT_FALLBACK in stt.PROVIDER_NAMES


def test_an_absent_language_means_detect_rather_than_a_default_value():
    # STT_LANGUAGE is the one variable here whose empty value is the *setting*
    # rather than a missing one, so it needs its own parser -- `_string` would
    # substitute a default and there is no sensible default language to
    # substitute. All three shapes of absence resolve the same way.
    for env in (MINIMAL, MINIMAL | {"STT_LANGUAGE": ""}, MINIMAL | {"STT_LANGUAGE": "   "}):
        assert Settings.from_env(env).stt_language is None


def test_a_named_language_is_taken_and_trimmed():
    # Trimmed because this arrives from a hand-edited .env, and faster-whisper
    # matches the code exactly -- ` en` would be rejected deep inside the library
    # on the first voice memo rather than here.
    assert Settings.from_env(MINIMAL | {"STT_LANGUAGE": " en "}).stt_language == "en"


def test_the_default_model_is_carried_verbatim():
    # Not validated at parse time, on purpose: the valid set belongs to
    # faster-whisper and importing it into config parsing would cost a second of
    # startup on every boot. config.py says so at the constant.
    assert Settings.from_env(MINIMAL).stt_model == DEFAULT_STT_MODEL
    assert Settings.from_env(MINIMAL | {"STT_MODEL": "tiny"}).stt_model == "tiny"

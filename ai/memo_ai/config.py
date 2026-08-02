"""
The environment, parsed once into one frozen object.

Two parsing rules are lifted from the API's ``App\\Support\\Env`` rather than
reinvented. Both runtimes read the same variables out of the same
docker-compose.yml, and two runtimes disagreeing about what a value *means* is a
worse outcome than either reading on its own:

  1. An empty string is an absence, not a value. ``docker run -e STT_PROVIDER=``
     and a commented-out line in someone's ``.env`` both produce ``''``, and
     ``os.environ.get("STT_PROVIDER", "local")`` hands back that ``''`` because
     the default only applies when the key is missing outright.
     docker-compose.yml uses ``${VAR:-default}`` -- never ``${VAR-default}`` --
     throughout for exactly this reason, and .env.example says so at the
     variable.

  2. A number this process cannot parse is a deployment mistake, not a zero.
     ``float("abc")`` already raises; what needs writing is the message, because
     the traceback from a bare ``float()`` names neither the variable nor the
     value that broke it.

Every default here is repeated from docker-compose.yml rather than derived from
it, the same way ``api/config/memo.php`` mirrors its own -- these values also
have to be right under a bare ``docker run`` with no compose file in sight.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Mirrors docker-compose.yml. `local` rather than `fake`, deliberately: the
# committed default must mean "real transcription" everywhere, so that a
# misconfigured deployment cannot silently serve canned text. As of MEMO-14 it
# does -- memo_ai/stt/local.py is faster-whisper, and it needs no key, no account
# and no network once the weights are cached.
DEFAULT_STT_PROVIDER = "local"

# Equal to the provider above, which memo_ai/stt/__init__.py collapses to a single
# provider rather than a chain that would call the same one twice on every
# failure. They only differ if somebody sets one of them.
DEFAULT_STT_FALLBACK = "local"

# Whisper size for the local provider. Not validated here, and that is the same
# rule the provider name does not follow: this one's valid set belongs to
# faster-whisper and changes with it, so checking it would mean importing the
# library into config parsing -- a second of startup, on every boot, to catch a
# typo that memo_ai/stt/local.py reports on the first voice memo with the
# library's own list in the log.
#
# `large-v3-turbo` rather than `base`, and it was `base` until a real person
# recorded into the real app. "I would like to place an order", spoken in an
# Indian accent, came back from `base` as "I would like to blaze a door there".
# `small` got it to "blaze an order" and `medium` got it right; only turbo also
# fixed the second recording in the same session. The README has the table.
#
# It is not the slow choice, which is the part worth knowing: turbo is a
# large-v3 encoder with a four-layer decoder, so it runs at 0.64x realtime here
# against `medium`'s 2.14x -- faster than `small`, and three times faster than
# the model it matches. What it costs is 1.6 GB on disk and about 1.1 GB
# resident per replica.
DEFAULT_STT_MODEL = "large-v3-turbo"

# Empty, meaning let whisper detect it per recording. Naming a language is worth
# roughly 30 percent of the job (9.3s against 6.4s on a three-second clip, five
# runs each) because detection is a whole extra encoder pass, and it removes a
# real failure: detection runs on one window and is not reliable on short or
# accented audio. Measured here -- three seconds of accented English came back
# `en` at 0.39 confidence, and one of the committed English fixtures is detected
# as Russian at 0.89. A wrong language token does not degrade a transcript, it
# ruins it.
#
# Still empty by default, because the app has no idea what its user speaks and a
# wrong pin is worse than a slow detect. Set it when you know.
DEFAULT_STT_LANGUAGE = None
DEFAULT_AUDIO_DIR = "/data/audio"

# Ten minutes, mirroring docker-compose.yml and .env.example. Enforced in the
# worker rather than at the API edge because it cannot be enforced there: the cap
# is a duration, and the duration of a browser recording is not known until
# ffmpeg has rewritten it (memo_ai/audio.py has the measurement). What the API
# caps instead is bytes -- a different limit, refusing a different thing, and the
# README explains why both exist.
DEFAULT_MAX_AUDIO_SECONDS = 600.0

# WORKER_POLL_SECONDS is mirrored in docker-compose.yml and .env.example like every
# other variable; LOG_LEVEL is deliberately not, because logging verbosity is a
# thing you set for one debugging session with `docker compose run` rather than a
# thing a deployment configures.
#
# One second is the idle-path sleep only: after a claim that finds work the worker
# loops back without waiting, so this bounds pickup latency, not throughput.
DEFAULT_POLL_SECONDS = 1.0

# Three attempts including the first, mirroring docker-compose.yml.
#
# The count is incremented by the claim statement rather than by the failure write
# (memo_ai/memos.py), which is what makes this bound hold through a SIGKILL: a memo
# claimed and destroyed three times carries `attempts = 3` on the row with no code
# of ours having run, and the reaper resolves it terminally instead of handing it
# to a fourth claim. A cap enforced on the way *out* of a job would count only the
# failures a job survived long enough to record, and a memo that kills its worker
# would be immortal.
DEFAULT_MAX_ATTEMPTS = 3

# The base of the exponential backoff written to `next_attempt_at`, doubling per
# attempt with jitter: roughly 30s before the second attempt and 60s before the
# third, so a poison memo is terminal inside two minutes of queue time.
#
# Chosen against the slowest retryable failure rather than against a poison memo,
# because that is the case the delay is actually for. `SttUnavailable` on a cold
# cache means the model is still downloading, and each attempt waits out its own
# MODEL_LOAD_TIMEOUT_SECONDS (300s) before saying so -- so the three attempts span
# about 16 minutes of wall clock, not 90 seconds, and a 1.6 GB fetch has that long
# to finish. A much larger base would only add idle time to that; a much smaller
# one would spend all three attempts inside a single download.
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0

# The claim lease: how long a row may sit in `processing` before the reaper takes
# it back. It must exceed the longest a healthy job can legitimately run, or the
# reaper requeues work that is still in progress -- so it is derived rather than
# picked, and memo_ai/pipeline.py's `job_budget_seconds` is the derivation.
#
# At the shipped defaults that budget is 2,880s: 180s of ffprobe and ffmpeg, 300s
# waiting for a model load, and 2,400s of decode deadline for a 600-second memo.
# 3,600 clears it by twelve minutes.
#
# The margin is not the interesting part -- the coupling is. Raise
# MAX_AUDIO_SECONDS and the budget moves with it, so this number has to move too.
# The worker recomputes the budget at boot and says so in the log rather than
# leaving that to whoever edits the .env; see `_warn_if_lease_is_too_short`.
DEFAULT_REAP_AFTER_SECONDS = 3600.0

# How often each replica looks for expired leases. Independent of the poll
# interval, which runs twice a second per replica -- reaping is a write against
# every `processing` row and there is nothing to gain from doing it at that rate.
# A minute is well under the lease it enforces, so the delay it adds to a reaped
# memo is noise beside the hour it already waited.
DEFAULT_REAPER_INTERVAL_SECONDS = 60.0

DEFAULT_LOG_LEVEL = "INFO"

# The five real levels, rather than logging.getLevelNamesMapping(), which was the
# first version of this and offers eight. The three it adds are all wrong to put in
# front of a user: `WARN` and `FATAL` are deprecated aliases, and `NOTSET` is not a
# verbosity at all -- it means "inherit", which on the root logger resolves to
# "everything" (checked: root.level 0, isEnabledFor(INFO) true). Listing it in the
# error message for a mistyped level recommends it.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """
    A refusal to start, raised before anything else is wired up.

    Distinct from every other exception in this package because of where it is
    handled: ``memo_ai/worker/__main__.py`` catches it, prints it plainly and
    exits 2, without a logger name or a traceback in front of it. A person
    reading ``docker compose logs ai-worker`` after mistyping a variable should
    see the variable and the value, nothing else.
    """


@dataclass(frozen=True)
class Settings:
    """Everything the worker reads from the environment, resolved and validated."""

    database_url: str
    audio_dir: Path
    stt_provider: str
    stt_fallback: str
    stt_model: str

    # None means "detect it", which is a value rather than a missing setting --
    # hence _optional below instead of _string, whose whole job is to turn an
    # empty variable back into its default.
    stt_language: str | None

    max_audio_seconds: float
    poll_seconds: float

    # The retry and reaper policy. Read together by memo_ai/memos.py's RetryPolicy
    # rather than one at a time, because they only make sense as a set: the lease
    # has to outlast a job, and the backoff has to fit inside the lease often
    # enough that three attempts are not three reaps.
    max_attempts: int
    retry_backoff_seconds: float
    reap_after_seconds: float
    reaper_interval_seconds: float

    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """
        ``env`` is a parameter rather than a read of ``os.environ`` so that the
        tests can cover the empty-string and bad-number cases without mutating
        the process they run in.
        """
        source = os.environ if env is None else env

        return cls(
            # No default. Every other value here has a sensible one; a
            # connection string does not, and inventing localhost would turn a
            # missing variable into a connection error against a database that
            # was never meant to be there.
            database_url=_required(source, "DATABASE_URL"),
            audio_dir=Path(_string(source, "AUDIO_DIR", DEFAULT_AUDIO_DIR)),
            stt_provider=_string(source, "STT_PROVIDER", DEFAULT_STT_PROVIDER),
            stt_fallback=_string(source, "STT_FALLBACK", DEFAULT_STT_FALLBACK),
            stt_model=_string(source, "STT_MODEL", DEFAULT_STT_MODEL),
            stt_language=_optional(source, "STT_LANGUAGE", DEFAULT_STT_LANGUAGE),
            # Float rather than int, and refused at zero for the same reason the
            # poll interval is: `MAX_AUDIO_SECONDS=0` is not a strict cap, it is a
            # configuration under which every voice memo fails and no text says
            # why. Fractional values are legal and are what the tests use.
            max_audio_seconds=_positive_float(
                source, "MAX_AUDIO_SECONDS", DEFAULT_MAX_AUDIO_SECONDS
            ),
            poll_seconds=_positive_float(source, "WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            # An int, and refused at zero for a reason the float version does not
            # have: `MAX_ATTEMPTS=0` is a configuration in which nothing is ever
            # retried *and* every claimed memo is immediately over the cap, so the
            # reaper resolves each one terminally the first time it looks. That is
            # a stack that transcribes nothing and blames the recordings.
            max_attempts=_positive_int(source, "MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            retry_backoff_seconds=_positive_float(
                source, "RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS
            ),
            reap_after_seconds=_positive_float(
                source, "REAP_AFTER_SECONDS", DEFAULT_REAP_AFTER_SECONDS
            ),
            reaper_interval_seconds=_positive_float(
                source, "REAPER_INTERVAL_SECONDS", DEFAULT_REAPER_INTERVAL_SECONDS
            ),
            log_level=_log_level(source, "LOG_LEVEL", DEFAULT_LOG_LEVEL),
        )


def _string(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)

    return value if value else default


def _optional(env: Mapping[str, str], key: str, default: str | None) -> str | None:
    """
    Like ``_string``, for a setting whose absence is itself the instruction.

    The two differ only in what they do with a default of ``None``, and keeping
    them separate is what stops rule 1 at the top of this file from quietly
    becoming rule 1-and-a-half: an empty variable is still an absence here, it is
    just that absence resolves to "no language" rather than to a fallback value.
    Merging this into ``_string`` would mean that function returning ``str |
    None``, and every existing caller having to prove it never can.
    """
    value = env.get(key)

    return value.strip() if value and value.strip() else default


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)

    if not value:
        raise ConfigError(f"{key} is not set. The worker cannot start without a database.")

    return value


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)

    if not raw:
        return default

    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}.") from None

    # Zero or negative is the case worth refusing rather than clamping. It is
    # not a slow poll, it is a loop with no sleep in it: two replicas issuing the
    # claim statement as fast as Postgres can answer, which looks like a database
    # problem rather than a typo in one variable.
    if value <= 0:
        raise ConfigError(f"{key} must be greater than zero, got {value}.")

    return value


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        # int() rather than int(float()), so `MAX_ATTEMPTS=3.5` is a refusal rather
        # than a silent 3. A fractional attempt count is a misunderstanding of the
        # setting, and rounding it teaches the misunderstanding.
        raise ConfigError(f"{key} must be a whole number, got {raw!r}.") from None

    if value <= 0:
        raise ConfigError(f"{key} must be greater than zero, got {value}.")

    return value


def _log_level(env: Mapping[str, str], key: str, default: str) -> str:
    name = _string(env, key, default).upper()

    # Refused rather than defaulted. getLevelName() answers a *string* ("Level INF")
    # for an unknown name instead of raising, so `LOG_LEVEL=INF` would otherwise
    # survive all the way to a logger that emits nothing.
    if name not in LOG_LEVELS:
        raise ConfigError(f"{key} must be one of {', '.join(LOG_LEVELS)}, got {name!r}.")

    return name

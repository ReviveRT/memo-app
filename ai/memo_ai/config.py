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

# Whisper size for the local provider: tiny | base | small | medium. Not validated
# here, and that is the same rule the provider name does not follow: this one's
# valid set belongs to faster-whisper and changes with it, so checking it would
# mean importing the library into config parsing -- a second of startup, on every
# boot, to catch a typo that memo_ai/stt/local.py reports on the first voice memo
# with the library's own list in the log.
DEFAULT_STT_MODEL = "base"
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
    max_audio_seconds: float
    poll_seconds: float
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
            # Float rather than int, and refused at zero for the same reason the
            # poll interval is: `MAX_AUDIO_SECONDS=0` is not a strict cap, it is a
            # configuration under which every voice memo fails and no text says
            # why. Fractional values are legal and are what the tests use.
            max_audio_seconds=_positive_float(
                source, "MAX_AUDIO_SECONDS", DEFAULT_MAX_AUDIO_SECONDS
            ),
            poll_seconds=_positive_float(source, "WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            log_level=_log_level(source, "LOG_LEVEL", DEFAULT_LOG_LEVEL),
        )


def _string(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)

    return value if value else default


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


def _log_level(env: Mapping[str, str], key: str, default: str) -> str:
    name = _string(env, key, default).upper()

    # Refused rather than defaulted. getLevelName() answers a *string* ("Level INF")
    # for an unknown name instead of raising, so `LOG_LEVEL=INF` would otherwise
    # survive all the way to a logger that emits nothing.
    if name not in LOG_LEVELS:
        raise ConfigError(f"{key} must be one of {', '.join(LOG_LEVELS)}, got {name!r}.")

    return name

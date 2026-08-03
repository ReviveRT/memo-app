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

**One Settings for two services**, as of MEMO-24. ``ai-worker`` and ``ai-api`` are
the same image with different entrypoints, so they read the same environment and
parse it the same way; splitting this into a worker half and an ask half would
mean two files that both have to agree with docker-compose.yml about
``DATABASE_URL`` and ``ENRICH_MODEL_PATH``. What each process actually *uses* is a
subset, and that is a property of the entrypoint rather than of the environment --
``ai-api`` never reads ``MAX_ATTEMPTS`` and the worker never reads ``ASK_TOP_K``.
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

# The model `STT_PROVIDER=groq` asks for, mirroring docker-compose.yml.
#
# Its own variable rather than a reuse of `STT_MODEL`, because the two name the
# same weights in different namespaces: faster-whisper calls it `large-v3-turbo`
# and Groq calls it `whisper-large-v3-turbo`. One variable would mean switching
# providers silently requested a model the other has never heard of.
DEFAULT_GROQ_STT_MODEL = "whisper-large-v3-turbo"

# What `AUDIO_BUCKET_REGION` means when it is not set. Cloudflare R2 requires the
# literal "auto" and ignores it; real S3 needs its own, so this is the default that
# makes the intended target work without configuration. api/config/filesystems.php
# carries the same default for the same reason.
DEFAULT_AUDIO_BUCKET_REGION = "auto"

# Everything a bucket needs beyond its name. Named here rather than inline in the
# check below so the error message and the parsing cannot drift apart.
REQUIRED_WITH_BUCKET = (
    "AUDIO_BUCKET_ENDPOINT",
    "AUDIO_BUCKET_KEY",
    "AUDIO_BUCKET_SECRET",
)

# Mirrors docker-compose.yml. `local` rather than `none`, on the same argument
# DEFAULT_STT_PROVIDER makes: the committed default must mean "the feature works",
# because a stack that silently ships without titles and summaries is
# indistinguishable from one whose enrichment is broken.
#
# `none` is the other name, and it is a real configuration rather than a way to
# turn the feature off in anger -- the local model adds about 1.7 GB to a
# worker's RSS on the first memo that needs it, of which ~0.6 GB is per-replica
# and the rest is a shared mapping of the weight file. On a small machine that
# is worth declining. See memo_ai/enrich/__init__.py for the set.
DEFAULT_ENRICH_PROVIDER = "local"

# Where ai/Dockerfile bakes the enrichment weights (MEMO-15).
#
# Read from the environment, unlike memo_ai/stt/local.py's BAKED_MODEL_DIR, which
# is a literal on the argument that it is a fact about the image rather than a
# knob. The difference is that this one is a *filename* and the whisper one is a
# directory named after STT_MODEL: this path ends in the GGUF that
# `--build-arg ENRICH_MODEL_FILE` chose, which nothing at runtime could
# reconstruct. So the Dockerfile writes the answer into the image as an ENV and
# this reads it, which is the contract that file states at the line.
#
# The default repeats what the shipped build args produce, like every other
# default here, so a bare `docker run` of an image built without changes still
# finds its model.
DEFAULT_ENRICH_MODEL_PATH = "/opt/models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf"

# --- Ask my memos (MEMO-24), read by ai-api and by nothing in the worker -----
#
# Where uvicorn listens is **deliberately not here**, and that is the one asymmetry
# in this file worth explaining rather than fixing. memo_ai/ask/__main__.py holds
# the host and the port as literals, on the same argument ai/Dockerfile makes about
# MODEL_DIR: as a variable it would be a foot-gun with no failure mode. The port is
# also written into docker-compose.yml's healthcheck and into `AI_API_URL`'s
# default, so `ASK_PORT=9000` would move the listener and leave both of those
# pointing at a closed socket -- a container that never reports healthy and a proxy
# that 503s, neither of which names the variable that did it. To run this service
# somewhere else, point `AI_API_URL` at it; that one *is* a variable.

# How many memos are put in front of the model.
#
# Three, at the bottom of the task's own "3 to 5" range, and the reason is that
# this is the one path in the stack a human waits on. Prompt processing dominates
# CPU inference here -- MEMO-21 measured 36 s for 10,000 characters against 2.4 s
# for 71 -- so the retrieved context *is* the latency, near enough linearly. Three
# memos at ASK_MEMO_CHARS below is about 3,600 characters of evidence; five would be
# 6,000 and would buy a second opinion nobody is still waiting for.
#
# It is a knob rather than a constant because the right answer depends on the
# machine. On something faster than the laptop these numbers came from, five is
# free. README.md says so next to the measurements.
DEFAULT_ASK_TOP_K = 3

# How much of one memo the model is shown.
#
# 1,200 characters, and the number is a budget rather than a preference: TOP_K
# times this, plus the instructions and the answer, has to stay inside the context
# the model is loaded with (memo_ai/ask/model.py sizes it from exactly these two).
#
# Well under MAX_TRANSCRIPT_CHARS on the enrichment side (10,000), and that
# asymmetry is the point. Enrichment reads one memo and nobody is waiting; this
# reads several and somebody is. What keeps 1,200 characters from being an
# arbitrary cut is that the excerpt is chosen by `ts_headline` around the words the
# question asked about, not taken off the front -- see memo_ai/ask/retrieval.py.
DEFAULT_ASK_MEMO_CHARS = 1200

# How long one answer may take before the question gives up on it.
#
# Three minutes, against a measured worst case well under it, and loose for the
# same reason memo_ai/enrich/local.py's DEADLINE_SECONDS is loose: it exists to stop
# a wedged generation holding the one model this service has, not to enforce a
# latency target. A reviewer's laptop under load is several times slower than the
# machine these numbers came from without being broken.
#
# What bounds the *felt* wait is not this: the answer streams, so the client sees
# words as they are produced and can stop reading whenever it likes.
DEFAULT_ASK_DEADLINE_SECONDS = 180.0

# `local`, matching DEFAULT_STT_PROVIDER and DEFAULT_ENRICH_PROVIDER and for the same
# stated reason: the committed default has to mean "this works with no account and no
# key", and under compose the weights are already baked into the ai image.
DEFAULT_ASK_PROVIDER = "local"

# What `ASK_PROVIDER=groq` asks for by default.
#
# An 8B model rather than one of the large ones, and the size is chosen against the
# job rather than for economy. This prompt is extractive: memo_ai/ask/prompt.py fences
# three memos and instructs the model to answer only from them and cite them by
# number. That is a reading-comprehension task over ~3,600 characters with a
# 320-token ceiling on the reply -- which an 8B model does about as well as a 70B one,
# several times faster, and at a rate limit a free plan does not exhaust in a demo.
#
# It also comfortably beats what it replaces. The local backend is Qwen2.5-1.5B, so
# `groq` is a larger model than the default it stands in for, not a smaller one.
DEFAULT_GROQ_ASK_MODEL = "llama-3.1-8b-instant"

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
# At the shipped defaults that budget is 3,300s: 180s of ffprobe and ffmpeg, 300s
# waiting for a model load, 2,400s of decode deadline for a 600-second memo, and
# 420s of enrichment. 3,600 clears it by five minutes -- or by twelve on
# ENRICH_PROVIDER=none, which drops the last term.
#
# The margin is not the interesting part -- the coupling is. Raise
# MAX_AUDIO_SECONDS and the budget moves with it, and so does switching
# ENRICH_PROVIDER, so this number has to move too.
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

    # `STT_PROVIDER=groq`, which is opt-in and off by default.
    #
    # **The key is optional here on purpose.** Every other absent setting in this
    # class has a default that works; this one cannot, and refusing to boot without
    # it would mean a stack configured for `local` could be taken down by an
    # unrelated variable. `None` reaches memo_ai/stt/groq.py, which fails the first
    # *voice memo* with a sentence naming the variable -- and falls back to the
    # local model on the shipped `STT_FALLBACK`.
    groq_api_key: str | None
    groq_stt_model: str

    # Where recordings live when this deployment keeps them in a bucket rather than
    # on the shared `audio` volume (memo_ai/blobs.py has why that became necessary).
    #
    # **`audio_bucket` alone decides**, matching the API's AppServiceProvider: a
    # driver name would be a second way to spell the same fact and a first way to
    # spell a contradiction. Empty means the volume, which is what local compose and
    # every test get without setting anything.
    #
    # All optional, so a stack using the volume is not asked for credentials it has
    # no use for. A bucket named without the rest of these is caught at parse time by
    # the check in `from_env` rather than on the first voice memo -- a half-configured
    # bucket is a deployment mistake, and the worker should refuse to start rather
    # than fail memos one at a time.
    audio_bucket: str | None
    audio_bucket_endpoint: str | None
    audio_bucket_region: str
    audio_bucket_key: str | None
    audio_bucket_secret: str | None

    # The enrichment pass (MEMO-21). Two settings and no fallback name, unlike
    # transcription: there is one enricher and giving up on it is not a failure,
    # so there is nothing for a second provider to be a second opinion about.
    enrich_provider: str
    enrich_model_path: Path

    # Ask my memos (MEMO-24). Read by ai-api alone -- the worker parses them and
    # never looks at them, which is what one Settings for two entrypoints costs and
    # is cheaper than two parsers disagreeing about DATABASE_URL.
    #
    # There is no `ask_model_path`: ai-api opens `enrich_model_path`, the same GGUF
    # the worker enriches with. Two settings for one file would let a deployment
    # point them at different models and then wonder why a summary and an answer
    # about the same memo disagree. There is no `ask_host` or `ask_port` either --
    # see the section above for why those are literals.
    ask_top_k: int
    ask_memo_chars: int
    ask_deadline_seconds: float

    # Which backend answers. `local` opens `enrich_model_path` as described above;
    # `groq` sends the same prompt to a hosted model and opens nothing.
    #
    # `local` is the committed default for the reason DEFAULT_STT_PROVIDER is: the
    # shipped configuration must mean "the feature works with no account and no key",
    # and under compose the weights are already in the image. A deployment that
    # cannot afford 1.1 GB of them sets this instead -- see deploy/Dockerfile, which
    # is the only place in the repository that does.
    #
    # Not validated against a list here, and that is the same deliberate choice
    # STT_PROVIDER makes: the factory in memo_ai/ask/__main__.py refuses an unknown
    # name with a sentence that lists the ones it has, which is a better error than
    # a parse failure naming a set.
    ask_provider: str

    # The model `ASK_PROVIDER=groq` asks for. Groq's catalogue moves faster than this
    # repository does -- models arrive, get renamed and get retired -- so this is a
    # variable with a default rather than a constant, and a deployment whose default
    # has been retired can move without a rebuild.
    groq_ask_model: str

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

        # Before anything is constructed, so a half-configured bucket is one error naming
        # the variables rather than a Settings that looks fine until the first voice memo.
        _check_bucket(source)

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
            # `_optional`, so an unset variable and `GROQ_API_KEY=` are the same
            # absence -- which is rule 1 at the top of this file, and matters more
            # here than anywhere else: `docker compose up` with no `.env` passes an
            # empty string, and a provider that treated `''` as a key would send it
            # to Groq and get a 401 back instead of the sentence naming the variable.
            groq_api_key=_optional(source, "GROQ_API_KEY", None),
            groq_stt_model=_string(source, "GROQ_STT_MODEL", DEFAULT_GROQ_STT_MODEL),
            # `_optional` throughout, so an unset variable and an empty one are the
            # same absence -- rule 1 again, and it is what makes `audio_bucket` a
            # usable switch: `docker compose up` with no `.env` passes empty strings
            # for all of these, and a truthiness test on `''` would read as "bucket
            # configured" and send every worker looking for a bucket named nothing.
            audio_bucket=_optional(source, "AUDIO_BUCKET", None),
            audio_bucket_endpoint=_optional(source, "AUDIO_BUCKET_ENDPOINT", None),
            # 'auto' is what R2 requires and ignores. Real S3 needs its own, so this
            # is a setting with R2's answer as the default -- matching the `audio`
            # disk in api/config/filesystems.php, which has the same default for the
            # same reason.
            audio_bucket_region=_string(source, "AUDIO_BUCKET_REGION", DEFAULT_AUDIO_BUCKET_REGION),
            audio_bucket_key=_optional(source, "AUDIO_BUCKET_KEY", None),
            audio_bucket_secret=_optional(source, "AUDIO_BUCKET_SECRET", None),
            # Not validated here, deliberately, and the same way STT_PROVIDER is
            # not: the set of names lives in memo_ai/enrich/__init__.py beside the
            # classes it maps to, and a second copy of it in config parsing is a
            # second thing to forget. The worker resolves the name at boot and
            # exits 2 on an unknown one, which is the same outcome one line later.
            enrich_provider=_string(source, "ENRICH_PROVIDER", DEFAULT_ENRICH_PROVIDER),
            enrich_model_path=Path(
                _string(source, "ENRICH_MODEL_PATH", DEFAULT_ENRICH_MODEL_PATH)
            ),
            # Positive ints for the same reason MAX_ATTEMPTS is one: `ASK_TOP_K=0`
            # is not a narrow search, it is a service that retrieves nothing and
            # answers "no memo mentions that" to every question ever asked.
            ask_top_k=_positive_int(source, "ASK_TOP_K", DEFAULT_ASK_TOP_K),
            ask_memo_chars=_positive_int(source, "ASK_MEMO_CHARS", DEFAULT_ASK_MEMO_CHARS),
            ask_deadline_seconds=_positive_float(
                source, "ASK_DEADLINE_SECONDS", DEFAULT_ASK_DEADLINE_SECONDS
            ),
            ask_provider=_string(source, "ASK_PROVIDER", DEFAULT_ASK_PROVIDER),
            groq_ask_model=_string(source, "GROQ_ASK_MODEL", DEFAULT_GROQ_ASK_MODEL),
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

    def uses_bucket(self) -> bool:
        """
        Whether recordings live in a bucket rather than on ``audio_dir``.

        One question with one answer, asked by ``pipeline.owed_audio``. A method rather
        than a truthiness test at the call site so that "what counts as configured" is
        decided here, next to the check below that guarantees the rest of the settings are
        present whenever this is true.
        """
        return bool(self.audio_bucket)


def _check_bucket(source: Mapping[str, str]) -> None:
    """
    Refuse a half-configured bucket at parse time.

    A bucket named with no endpoint or no credentials is a deployment mistake, and the
    alternative to catching it here is catching it once per voice memo: each one claims,
    fails to sign or fails to connect, and lands on the row as an error about the bucket
    being unreachable. That is a slow, expensive and misleading way to report a typo in an
    environment variable.

    Only checked when a bucket is named, so a stack using the volume -- local compose,
    every test -- is never asked for credentials it has no use for.
    """
    if not source.get("AUDIO_BUCKET"):
        return

    missing = [key for key in REQUIRED_WITH_BUCKET if not source.get(key)]

    if missing:
        raise ConfigError(
            f"AUDIO_BUCKET is set, so {', '.join(missing)} must be set too. "
            "Unset AUDIO_BUCKET to keep recordings on the audio volume."
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
        # "Nothing here" rather than "the worker", which is what this said until
        # `Settings.from_env` stopped having one caller. It now has three, from two
        # tasks that landed together: `python -m memo_ai.costs` (MEMO-22) and
        # `python -m memo_ai.ask` (MEMO-24) both print this sentence under their own
        # prefix, and `ai-api: ... the worker cannot start` sends the reader to look
        # at the wrong container.
        raise ConfigError(f"{key} is not set. Nothing in this package runs without a database.")

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

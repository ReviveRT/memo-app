"""Wiring, signals, and the claim loop."""

import logging
import random
import signal
import sys
import threading
import time

import psycopg

from memo_ai import audio, db, enrich, log, pipeline, rss, stt
from memo_ai.config import ConfigError, Settings
from memo_ai.enrich import Enricher
from memo_ai.memos import MemoQueue, Reaped, RetryPolicy

logger = logging.getLogger("memo_ai.worker")

# How long to wait before reconnecting after Postgres goes away. Short, because
# the common cause is a `docker compose restart db` and the right behaviour is to
# be back before anyone reloads the page.
RECONNECT_SECONDS = 2.0

# Exit codes. 2 for a configuration the worker refuses, distinct from 1, so that a
# `docker compose ps` reading `exited (2)` says "read the first line of the log"
# rather than "something crashed".
EXIT_MISCONFIGURED = 2


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        # print, not logging: logging is not configured yet (its level comes from
        # the settings that just failed to parse), and a configuration error should
        # read as a sentence rather than arrive behind a timestamp and a logger
        # name.
        #
        # Under compose this is also a restart loop, because the service is
        # `restart: unless-stopped` -- the same message every couple of seconds.
        # That is the intended outcome: the alternative is a worker that starts
        # with a value nobody chose.
        print(f"ai-worker: {error}", file=sys.stderr)

        return EXIT_MISCONFIGURED

    log.configure(settings.log_level)

    try:
        provider = stt.resolve_chain(settings)
        # Same handler as the provider above, because the two failures are the same
        # failure: a name in the environment that this build has no class for. It
        # is the *only* way enrichment may stop the boot -- everything after this
        # point costs a memo its summary and nothing more.
        enricher = enrich.resolve(settings)
    except ConfigError as error:
        logger.error("%s", error)

        return EXIT_MISCONFIGURED

    shutdown = threading.Event()
    _install_signal_handlers(shutdown)

    # The settings rather than `provider.name`, which is no longer the same thing:
    # a chain names both of its members and a collapsed one names neither variable.
    # What a reader of this line wants is what the *configuration* says, so that it
    # can be compared against a .env -- which provider actually produced a given
    # transcript is a property of that memo and is recorded on its row.
    #
    # `stt_model` is logged too, as of MEMO-14. It is inert on `fake` and the whole
    # of the local provider's cost and quality, and nothing else in the logs would
    # say which model a slow transcription was running.
    # `rss` is the baseline for MEMO-22's memory figures, and it is only meaningful
    # because it is taken *here* -- before either model is touched. Both load
    # lazily, so this line is the 18 MB an idle worker costs, and every later `rss=`
    # on a ready memo is measured against it. Without the baseline in the same log,
    # "1,708 MB" is a number with nothing to compare it to.
    #
    # `describe` and not `brief`, which is the one place the shared/private split is
    # worth its page-table walk: this process is 18 MB, so the walk is free here and
    # costs 10.8 ms once the models are in. The split does not change per memo, so
    # stating it once at boot is all any reader needs -- memo_ai/rss.py has both
    # measurements and memo_ai/pipeline.py takes the cheap reading thereafter.
    logger.info(
        "ai-worker starting: stt_provider=%s stt_fallback=%s stt_model=%s stt_language=%s "
        "enrich_provider=%s audio_dir=%s max_audio=%.0fs poll=%.1fs attempts=%d "
        "backoff=%.0fs lease=%.0fs rss=%s",
        settings.stt_provider,
        settings.stt_fallback,
        settings.stt_model,
        settings.stt_language or "auto",
        # The name and not the path, which would put a 60-character filename in
        # the middle of the one line somebody reads to check their .env. The path
        # is logged by the enricher when it loads, which is when it matters.
        settings.enrich_provider,
        settings.audio_dir,
        settings.max_audio_seconds,
        settings.poll_seconds,
        settings.max_attempts,
        settings.retry_backoff_seconds,
        settings.reap_after_seconds,
        rss.describe(),
    )

    _warn_if_lease_is_too_short(settings, enricher)

    # Start fetching the model now rather than on the first voice memo. Optional
    # on the protocol, so only a provider that has something to warm does
    # anything -- `fake` has no such method and this is a no-op for it.
    #
    # Deliberately after the line above. A 1.6 GB download logs its own progress
    # through huggingface_hub, and having that arrive before the worker has said
    # what it is configured as would bury the one line that explains why it is
    # downloading anything.
    prefetch = getattr(provider, "prefetch", None)

    if prefetch is not None:
        prefetch()

    # **The enricher is deliberately not warmed alongside it**, and the asymmetry
    # is a decision rather than an oversight. Whisper prefetches because its
    # weights may still be downloading and whoever records first pays for that; the
    # enrichment weights are baked into the image, so there is nothing to race and
    # nothing to warm. What loading it lazily buys instead is memory -- a replica
    # that only ever transcribes, or only ever takes text memos, never pays the
    # second model's 1.7 GB. memo_ai/enrich/local.py has the rest of that
    # argument, and this is the line somebody would otherwise add.

    # A warning, not a refusal. Text memos never reach ffmpeg, so a worker without
    # it still drains half the queue -- and MEMO-08's rule, set by UnimplementedStt,
    # is that a missing capability fails the memo that needs it rather than the boot
    # that might not. `restart: unless-stopped` would turn the alternative into a
    # restart loop that takes text memos down too. Logged at boot anyway, because the
    # per-memo message is only seen by whoever recorded that memo.
    if not audio.ffmpeg_available():
        logger.warning(
            "ffmpeg or ffprobe is not on PATH -- voice memos will fail until it is. "
            "The image built by ai/Dockerfile has both."
        )

    _run(settings, provider, enricher, shutdown)

    logger.info("ai-worker stopped")

    return 0


def _warn_if_lease_is_too_short(settings: Settings, enricher: Enricher) -> None:
    """
    Compare the configured lease against what a job can actually take, and say so.

    ``REAP_AFTER_SECONDS`` has one hard constraint -- it must exceed the whole-job
    deadline -- and getting it wrong is the quiet kind of wrong. A lease under the
    budget does not error; it reaps healthy long jobs, which looks like
    transcription being flaky on exactly the recordings that take longest, and the
    row it leaves behind says it was "interrupted" with nothing to say by what.

    Checked at boot rather than written into a comment because the budget is not a
    constant: it scales with ``MAX_AUDIO_SECONDS``, so somebody raising the
    duration cap invalidates a lease that was correct when it was chosen. This is
    the line that tells them.

    The enricher is passed for the same reason, and MEMO-21 is what made it a
    second variable: switching ``ENRICH_PROVIDER`` between ``local`` and ``none``
    moves this budget by 420 seconds, so a lease that clears one may not clear the
    other. Passing the resolved enricher rather than the setting means the check
    asks the object what it can spend instead of keeping its own table of what each
    name costs.

    A warning and not a refusal. The stack still works with a short lease -- memos
    are retried rather than lost, because the transcript commit means a reaped job
    resumes rather than restarts -- and refusing to boot over a tuning number would
    take the whole queue down, including the text memos that never come near any of
    these deadlines.
    """
    budget = pipeline.job_budget_seconds(settings.max_audio_seconds, enricher)

    if settings.reap_after_seconds > budget:
        return

    logger.warning(
        "REAP_AFTER_SECONDS is %.0fs but one job can legitimately take up to %.0fs at "
        "MAX_AUDIO_SECONDS=%.0f -- the reaper will requeue jobs that are still running. "
        "Raise it above %.0fs, or lower MAX_AUDIO_SECONDS.",
        settings.reap_after_seconds,
        budget,
        settings.max_audio_seconds,
        budget,
    )


def _run(
    settings: Settings,
    provider: stt.SttProvider,
    enricher: Enricher,
    shutdown: threading.Event,
) -> None:
    """
    Claim, work, write, repeat -- across as many connections as it takes.

    The outer loop exists for the connection and nothing else. Without it, a
    ``docker compose restart db`` kills both replicas and leaves recovery to
    Docker's restart policy, which works but reads in the logs like the worker
    crashed.
    """
    policy = RetryPolicy.from_settings(settings)

    # Outside the connection loop, so a database that keeps dropping does not turn
    # into a reaper that runs every two seconds. Zero means "the first pass is due
    # now": a replica coming up after a crash should take back what the crash
    # abandoned rather than wait out an interval first.
    next_reap = 0.0

    while not shutdown.is_set():
        try:
            with db.connect(settings) as connection:
                queue = MemoQueue(connection, policy)
                logger.info("connected to postgres, polling for queued memos")

                while not shutdown.is_set():
                    # Before the claim rather than on the idle path, so a replica
                    # that never finds an empty queue still reaps. A busy stack is
                    # where an abandoned row is least likely to be noticed and most
                    # likely to matter.
                    if time.monotonic() >= next_reap:
                        _reap(queue)
                        next_reap = time.monotonic() + settings.reaper_interval_seconds

                    memo = queue.claim()

                    if memo is None:
                        shutdown.wait(_poll_delay(settings.poll_seconds))

                        continue

                    logger.info(
                        "claimed memo %s (source=%s, attempt %d of %d)",
                        memo.id,
                        memo.source,
                        memo.attempts,
                        settings.max_attempts,
                    )
                    pipeline.run_job(
                        queue,
                        memo,
                        provider,
                        settings.audio_dir,
                        settings.max_audio_seconds,
                        # For AUDIO_BUCKET alone -- pipeline.available consults it to
                        # decide whether this recording is on the volume or in a bucket.
                        # audio_dir stays a parameter of its own because the tests drive
                        # it independently of any Settings.
                        settings,
                        # Whatever ENRICH_PROVIDER resolved to at boot -- the local
                        # model by default, `NO_ENRICHMENT` on `none`. Passed
                        # explicitly rather than left to the default, which is the
                        # arrangement that made MEMO-21 a change on this line.
                        enricher,
                    )

                    # No sleep on the success path, on purpose: after a claim that
                    # found work the queue is more likely than usual to hold more,
                    # and the poll interval is there for an empty queue rather than
                    # as a rate limit.

        # OperationalError only, deliberately not psycopg.Error. A broken connection
        # and an unreachable server both arrive as this one -- confirmed by
        # restarting the db container under a running worker, which logged
        # "terminating connection due to administrator command" and reconnected on
        # the next cycle. A mistake in one of the statements would be a
        # ProgrammingError instead, and that must crash rather than be retried every
        # two seconds forever behind a message about the connection.
        except psycopg.OperationalError as error:
            if shutdown.is_set():
                break

            # "unavailable" rather than "lost": this same handler covers the very
            # first connect, and a worker started against a wrong DATABASE_URL
            # reporting a lost connection sends the reader looking for a network
            # blip that never happened.
            #
            # A single line, not a traceback. Every cause of this is external and the
            # stack says nothing a reader can act on.
            logger.warning(
                "postgres unavailable (%s), retrying in %.1fs",
                _first_line(error),
                RECONNECT_SECONDS,
            )
            shutdown.wait(RECONNECT_SECONDS)


def _reap(queue: MemoQueue) -> None:
    """
    One reaper pass, logged only when it found something.

    Silent on the ordinary pass, which is every pass on a healthy stack. A line a
    minute per replica saying nothing was abandoned would bury the one that says
    something was -- and the ids are the point of that line, because they are what
    somebody then looks up.
    """
    reaped: Reaped = queue.reap()

    if not reaped:
        return

    if reaped.requeued:
        logger.warning(
            "reaped %d memo(s) whose claim expired, requeued for another attempt: %s",
            len(reaped.requeued),
            ", ".join(str(memo_id) for memo_id in reaped.requeued),
        )

    if reaped.failed:
        logger.error(
            "reaped %d memo(s) out of attempts with no transcript, marked failed: %s",
            len(reaped.failed),
            ", ".join(str(memo_id) for memo_id in reaped.failed),
        )

    if reaped.salvaged:
        # A different level from the two above, because the outcome is different:
        # nothing was lost. The transcript was committed before the interruptions
        # and the memo is published with it -- only the title and summary are
        # missing, which is what `enrichment_error` on the row now says.
        logger.warning(
            "reaped %d memo(s) out of attempts but already transcribed, published "
            "without enrichment: %s",
            len(reaped.salvaged),
            ", ".join(str(memo_id) for memo_id in reaped.salvaged),
        )


def _install_signal_handlers(shutdown: threading.Event) -> None:
    """
    Turn SIGTERM and SIGINT into a flag the loop reads between jobs.

    This is load-bearing twice over, and the second reason is the container one.
    The worker is PID 1 under compose, and the kernel does not apply default signal
    dispositions to PID 1 -- a SIGTERM with no handler installed is *ignored*, not
    fatal. Checked on this image rather than taken from the folklore: a container
    running `python -c "time.sleep(600)"` was still running after an explicit
    `docker kill -s TERM`.

    So without this handler the worker is never asked to stop, only killed. With it,
    `docker compose stop ai-worker` takes 0.3s and both replicas exit 0 after
    logging their last line; with the command swapped for a handler-less sleep, the
    same call exits 137. 137 is a SIGKILL, which is the part that matters: the
    in-flight job is destroyed rather than finished.

    The first reason is what it does with the flag. Shutdown stops the worker
    *claiming*; the job already in flight runs to completion and writes its result.
    That is now an optimisation rather than the only thing standing between a memo
    and permanent limbo -- the reaper takes back an abandoned claim after the lease
    -- but it is the difference between a `docker compose down` costing nothing and
    costing an hour of nothing happening to somebody's memo.

    How long that grace lasts is not this file's decision and turned out not to be
    the documented one either. The Compose spec gives `stop_grace_period` a default
    of 10s; measured on Compose v5.0.2, an unset grace period SIGKILLs a
    handler-less container after **1.2s**, while an explicit `stop_grace_period: 10s`
    takes 10.2s. That gap was worth setting the value explicitly rather than
    inheriting it, and docker-compose.yml says so at the line.

    It stopped being academic with MEMO-13. A job used to be a claim and a fake
    provider call -- about 4 ms -- and it now runs ffmpeg first: roughly 300 ms for
    a few seconds of audio, and 11.2 s measured on the longest recording the byte
    cap can admit. The inherited ~1.2s window would already be too short for a
    long memo; the explicit 30s is not.

    A SIGKILL still leaves the row in `processing`, and so does any job that outlives
    whatever that grace period is. Those are the reaper's cases, and the reason it is
    on MEMO-16's list rather than optional.

    The handler only sets the event: no logging inside it. A signal handler runs
    between bytecode instructions in the main thread, so one that logs can be
    entered while the logging module's own lock is held, and the deadlock that
    follows would look exactly like a worker hanging on shutdown.
    """

    def handle(signum: int, _frame: object) -> None:
        shutdown.set()

    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, handle)


def _poll_delay(base: float) -> float:
    """
    Jitter on the empty-queue sleep.

    Not a correctness measure -- ``SKIP LOCKED`` is what makes concurrent claims
    safe, and it was verified doing so. This only keeps two replicas that started
    within a second of each other from waking in lockstep for the lifetime of the
    stack, so their empty polls arrive at Postgres spread out rather than in pairs.
    """
    return base * random.uniform(0.8, 1.2)


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__


if __name__ == "__main__":
    sys.exit(main())

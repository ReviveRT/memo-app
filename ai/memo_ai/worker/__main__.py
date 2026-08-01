"""Wiring, signals, and the claim loop."""

import logging
import random
import signal
import sys
import threading

import psycopg

from memo_ai import audio, db, log, pipeline, stt
from memo_ai.config import ConfigError, Settings
from memo_ai.memos import MemoQueue

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
        provider = stt.resolve(settings.stt_provider, settings)
        # Not used by this task -- MEMO-14 wires the chain. Validated here so a typo
        # in it is caught on the boot after the edit.
        stt.require_known("STT_FALLBACK", settings.stt_fallback)
    except ConfigError as error:
        logger.error("%s", error)

        return EXIT_MISCONFIGURED

    shutdown = threading.Event()
    _install_signal_handlers(shutdown)

    logger.info(
        "ai-worker starting: stt_provider=%s audio_dir=%s max_audio=%.0fs poll=%.1fs",
        provider.name,
        settings.audio_dir,
        settings.max_audio_seconds,
        settings.poll_seconds,
    )

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

    _run(settings, provider, shutdown)

    logger.info("ai-worker stopped")

    return 0


def _run(settings: Settings, provider: stt.SttProvider, shutdown: threading.Event) -> None:
    """
    Claim, work, write, repeat -- across as many connections as it takes.

    The outer loop exists for the connection and nothing else. Without it, a
    ``docker compose restart db`` kills both replicas and leaves recovery to
    Docker's restart policy, which works but reads in the logs like the worker
    crashed.
    """
    while not shutdown.is_set():
        try:
            with db.connect(settings) as connection:
                queue = MemoQueue(connection)
                logger.info("connected to postgres, polling for queued memos")

                while not shutdown.is_set():
                    memo = queue.claim()

                    if memo is None:
                        shutdown.wait(_poll_delay(settings.poll_seconds))

                        continue

                    logger.info(
                        "claimed memo %s (source=%s, attempt %d)",
                        memo.id,
                        memo.source,
                        memo.attempts,
                    )
                    pipeline.run_job(
                        queue,
                        memo,
                        provider,
                        settings.audio_dir,
                        settings.max_audio_seconds,
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
    That matters more before MEMO-16 than after: there is no reaper yet, so a memo
    abandoned in `processing` is stuck for good, and a `docker compose down` in the
    middle of one would be the ordinary way to produce that.

    How long that grace lasts is not this file's decision and turned out not to be
    the documented one either. The Compose spec gives `stop_grace_period` a default
    of 10s; measured on Compose v5.0.2, an unset grace period SIGKILLs a
    handler-less container after **1.2s**, while an explicit `stop_grace_period: 10s`
    takes 10.2s. Since a job today finishes in about 4 ms either number is ample, but
    a 1.2s window would silently stop being ample the moment MEMO-14 makes
    transcription take seconds -- so docker-compose.yml now sets the value rather
    than inheriting it, and says why at the line.

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

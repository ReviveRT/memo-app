"""
``python -m memo_ai.ask`` -- the ai-api entrypoint.

The second entrypoint into the image ai/Dockerfile builds, which is what that file
means by "same image, different command". It shares the dependency set, the uid, the
baked weights and this package with ``python -m memo_ai.worker``; what it does not
share is a queue, because nothing here is queued.

Deliberately parallel to memo_ai/worker/__main__.py: parse the environment, refuse
to start on a value nobody chose, configure logging, say what was configured, then
run. A reader who has understood one of these two files has understood the other.
"""

import logging
import sys

import uvicorn

from memo_ai import log
from memo_ai.ask import model as ask_model
from memo_ai.ask.app import create_app
from memo_ai.ask.hosted import HostedModel
from memo_ai.config import ConfigError, Settings

logger = logging.getLogger("memo_ai.ask")

# Where uvicorn listens, and **literals rather than settings on purpose** --
# memo_ai/config.py has the argument at the section this would belong to. The short
# version: this port is also written into docker-compose.yml's healthcheck and into
# `AI_API_URL`'s default, so a variable that moved the listener would leave both
# pointing at a closed socket and name itself in neither failure.
#
# 0.0.0.0 rather than 127.0.0.1, because the only caller is the `api` container over
# the compose network and a loopback bind would refuse it. That is not a hole: no
# host port is mapped for this service, so the socket is reachable from that network
# and nowhere else, which is what keeps PHP the only public surface.
HOST = "0.0.0.0"  # noqa: S104 -- container-internal; see above
PORT = 8000

# 2 for a configuration this service refuses, distinct from 1, exactly as the worker
# uses it: `docker compose ps` reading `exited (2)` says "read the first line of the
# log" rather than "something crashed".
EXIT_MISCONFIGURED = 2


def _backend(settings: Settings) -> tuple[object, str]:
    """
    The model this service will answer with, and the phrase describing it for the log.

    A factory rather than a branch inside ``main``, so the unknown-name case has one
    place to be refused and the description cannot drift from the object that was
    actually built -- the start-up line is the only evidence a reader of the logs has
    about which backend is running.

    Deliberately shaped like ``memo_ai/stt/__init__.py``'s ``resolve``: an unknown
    provider is a ``ConfigError`` naming the ones that exist, because that is a
    sentence somebody can act on where a ``KeyError`` on a dict is not.
    """
    if settings.ask_provider == "groq":
        # No context check. `context_tokens` exists to keep ASK_TOP_K times
        # ASK_MEMO_CHARS inside a window llama.cpp has to allocate up front; a hosted
        # 128k-token model has no such ceiling, and enforcing the local one here would
        # refuse configurations this backend answers perfectly well.
        return (
            HostedModel(
                settings.groq_api_key,
                settings.groq_ask_model,
                deadline_seconds=settings.ask_deadline_seconds,
            ),
            f"provider=groq model={settings.groq_ask_model}",
        )

    if settings.ask_provider == "local":
        # Before anything is loaded and before the socket is bound, because this is
        # the one thing about an ask that cannot be fixed later: a context too small
        # for ASK_TOP_K memos fails on the first question with a ValueError out of
        # llama.cpp, which names neither variable.
        context = ask_model.context_tokens(settings.ask_top_k, settings.ask_memo_chars)

        # The path *is* logged, unlike in the worker's start-up line, and the asymmetry
        # is deliberate: the worker names the provider because a reader is checking it
        # against a .env, while this backend has one job and the file it opens is the
        # interesting fact about whether it can do it.
        return (
            ask_model.Model(
                settings.enrich_model_path,
                n_ctx=context,
                deadline_seconds=settings.ask_deadline_seconds,
            ),
            f"provider=local model={settings.enrich_model_path} context={context}",
        )

    raise ConfigError(
        f"ASK_PROVIDER is {settings.ask_provider!r}, which is not a provider this "
        "service has. Use 'local' to answer with the model in this image, or 'groq' "
        "to answer with a hosted one."
    )


def main() -> int:
    try:
        settings = Settings.from_env()
        model, described = _backend(settings)
    except ConfigError as error:
        # print rather than logging, for the reason the worker gives: logging is not
        # configured yet -- its level comes from the settings that just failed to
        # parse -- and a configuration error should read as a sentence rather than
        # arrive behind a timestamp and a logger name.
        print(f"ai-api: {error}", file=sys.stderr)

        return EXIT_MISCONFIGURED

    log.configure(settings.log_level)

    logger.info(
        "ai-api starting: listen=%s:%d %s top_k=%d memo_chars=%d deadline=%.0fs",
        HOST,
        PORT,
        described,
        settings.ask_top_k,
        settings.ask_memo_chars,
        settings.ask_deadline_seconds,
    )

    app = create_app(settings, model)

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        # One worker process, and it is not a default left alone: the model is
        # loaded per process, so two would be two copies of the KV cache and two
        # generations competing for the same four threads. Concurrency here is
        # bounded by the model, not by the socket.
        workers=1,
        # uvicorn installs its own handlers on the root logger otherwise, which
        # would undo memo_ai/log.py's format and timezone and leave ai-api's lines
        # looking nothing like the worker's in the same `docker compose logs`.
        log_config=None,
        # SIGTERM is handled by uvicorn itself: it stops accepting, lets in-flight
        # requests finish, and exits. That is the right shutdown for this service
        # and needs no equivalent of the worker's signal handling, because there is
        # no claim to release -- an interrupted answer leaves nothing behind.
        access_log=False,
    )

    logger.info("ai-api stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())

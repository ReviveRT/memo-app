"""
The ai-api service: two routes, one model, and no public surface of its own.

**Synchronous, and that is the contrast worth naming rather than an inconsistency.**
Everything else this stack does with a model is queued: a memo is stored, a worker
claims it, and the browser polls. That works because nobody is waiting -- the memo
is already saved and the transcript arrives when it arrives. A question has no such
row. There is nothing to poll for and nothing to come back to, so this one path
holds the request open and the architecture that saves the rest of the app does not
save it. NOTES.md discusses the trade where the two are compared.

**NDJSON rather than server-sent events.** Both stream; the difference is what the
two ends have to do. SSE's framing (``data:`` prefixes, blank-line records, an
``event:`` field) buys reconnection semantics through ``EventSource`` -- which is
GET-only, so a question in a request body could not use it and the browser would be
on ``fetch`` and a ``ReadableStream`` regardless. What is left is a frame format
that PHP has to understand in order to proxy it. One JSON object per line is a
format the proxy can pass through as bytes and the browser can split on ``\\n``, and
``curl`` reads it without a client library.

**No CORS, no host port, no authentication.** docker-compose.yml maps no port for
this service, so it is reachable from the compose network and nowhere else, and the
only thing on that network that calls it is the ``api`` container. The browser never
sees this origin -- that is the whole point of ``/api/ask`` being a proxy -- so
there is no cross-origin request to permit. Adding a permissive CORS policy "just in
case" would be adding the hole this design does not have.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from memo_ai import db
from memo_ai.ask import service
from memo_ai.ask.model import Model
from memo_ai.config import Settings

log = logging.getLogger(__name__)

# One JSON object per line. Registered as the response media type so a reader --
# `curl -i`, a network panel -- is told what it is looking at.
NDJSON = "application/x-ndjson"

# The cap on a question, in characters.
#
# Repeated on the PHP side (`AskRequest`), which is where it is actually enforced
# for the browser, and restated here because this service also has to be right when
# somebody calls it directly from inside the compose network. The same arrangement
# `MAX_TRANSCRIPT_CHARS` has with `StoreMemoRequest::MAX_TEXT_LENGTH`, and it fails
# the same safe way if the two drift: this one is the stricter reading, so the worst
# a longer cap over there can do is turn a 422 into a 422 from a different layer.
#
# 500 rather than something generous, because a question is a sentence. It is also a
# term in the context budget -- see OVERHEAD_TOKENS in memo_ai/ask/model.py.
MAX_QUESTION_CHARS = 500


class Question(BaseModel):
    """
    The request body. One field, and pydantic is what refuses everything else.

    ``min_length`` is 2 rather than 1 because a one-character question cannot
    produce a lexeme worth searching for, and rejecting it here is a clearer answer
    than the "that question has no words to search for" the service would otherwise
    give it.
    """

    question: str = Field(min_length=2, max_length=MAX_QUESTION_CHARS)


def create_app(settings: Settings, model: Model) -> FastAPI:
    """
    Build the app around an already-constructed model.

    Both are parameters rather than module-level singletons, which is what lets
    tests/test_ask_app.py drive every route against a fake model and a fake
    connection. memo_ai/ask/__main__.py is the one place the real ones are built.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Non-blocking: uvicorn binds and starts answering /health immediately, and
        # /health reports `loading` until the weights are in. A blocking load here
        # would make the container refuse connections for the first seconds of its
        # life, which reads to `docker compose ps` and to a healthcheck as a service
        # that is down rather than one that is starting.
        model.start_loading()

        yield

    app = FastAPI(
        title="memo-app ai-api",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> JSONResponse:
        """
        What this service can do right now, and the compose healthcheck's target.

        Both halves are reported because both can fail on their own and the
        remedies differ: a model that will not load is an image problem and a
        database that will not answer is a stack problem. The status code follows
        the pair, so the healthcheck needs no body parsing -- 200 only when a
        question could actually be answered.

        The same shape and the same argument as `GET /api/health` on the PHP side,
        which reports its own dependency rather than answering 200 because the
        process is up.
        """
        state = model.state
        database = _database_ok(settings)
        ready = state == "ready" and database

        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "model": state, "database": database},
        )

    @app.post("/ask")
    def ask(body: Question) -> StreamingResponse:
        """
        Answer one question over the memos.

        **Always 200, even when the answer is that there is no answer**, and the
        status is committed before the first byte -- see memo_ai/ask/service.py for
        why a failure that happens mid-answer arrives as an ``error`` event instead.
        The one thing that can still be a 4xx is the body, and pydantic answers
        that before this function runs.

        A connection per request rather than one held open. Two short statements
        per question against a service answering one question at a time is not a
        connection-pool problem, and the alternative -- a long-lived connection --
        would need the worker's reconnect loop for a service whose requests are
        minutes apart. `role="ask"` is what keeps these apart from the workers'
        in `pg_stat_activity`.
        """
        return StreamingResponse(
            _lines(settings, model, body.question),
            media_type=NDJSON,
            headers={
                # The answer is generated once and is never the same twice for a
                # different question; there is nothing here a cache could
                # legitimately serve. `no-store` rather than `no-cache` for the
                # reason `GET /api/memos` gives: revalidation would be reasonable,
                # a stored copy is not.
                "Cache-Control": "no-store",
                # Nginx and friends buffer a proxied response by default, which
                # would collect the whole answer and deliver it at the end -- the
                # exact behaviour streaming exists to avoid. Nothing in this stack
                # does that (the api container proxies with Guzzle and flushes per
                # chunk), and the header is one string against the day somebody puts
                # this behind something that would.
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _lines(settings: Settings, model: Model, question: str) -> Iterator[bytes]:
    """
    The service's events, one JSON object per line.

    Bytes rather than strings, so the encoding is decided here rather than by
    Starlette's default -- and ``ensure_ascii`` is left on, so a Russian memo's
    excerpt travels as escapes and the line is ASCII whatever the proxy in front of
    it assumes.

    **The connection is opened inside the generator, not before it.** A
    ``StreamingResponse`` does not start consuming until the response is being sent,
    so a connection acquired in the route function would be held open across
    whatever Starlette does in between. Here it is opened when the first event is
    wanted and closed by the ``with`` when the last one has gone out -- including
    the path where the client disconnected and the generator is closed early.
    """
    try:
        with db.connect(settings, role="ask") as connection:
            for event in service.answer(
                connection,
                model,
                question,
                top_k=settings.ask_top_k,
                memo_chars=settings.ask_memo_chars,
            ):
                yield _line({"type": event.type, **event.payload})
    except psycopg.OperationalError as error:
        # The one failure that can happen before any event has been emitted, and it
        # still cannot be a status code: `_lines` is only called once the response
        # has begun. So the database being unreachable is reported the same way a
        # model failure is, which is also what makes it visible to a reader rather
        # than arriving as a truncated stream.
        log.warning("postgres unavailable while answering: %s", error)

        yield _line(
            {
                "type": "error",
                "message": "The memo database is not answering. Check that the db "
                "container is up.",
            }
        )


def _line(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode()


def _database_ok(settings: Settings) -> bool:
    try:
        with db.connect(settings, role="ask") as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")

            return True
    except psycopg.Error as error:
        log.warning("health check could not reach postgres: %s", error)

        return False

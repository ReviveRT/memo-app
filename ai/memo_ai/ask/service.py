"""
One question, end to end: retrieve, then synthesise, as a sequence of events.

Separate from memo_ai/ask/app.py so that the *order of the answer* is testable
without a web server: what comes first, what a question with no evidence produces,
what a model failure looks like after the first byte has already gone out. The app
turns each event into a line of NDJSON and does nothing else.

**The event order is a contract.** ``sources`` always comes first and always comes
exactly once, before any text -- so a client can render the memos it is about to
read about while the model is still processing the prompt, which on this hardware
is most of the wait. Then zero or more ``token`` events. Then exactly one of
``done`` or ``error``.

**``error`` after the first token is a real case and the reason the stream carries
one at all.** An HTTP status is chosen before the first byte and cannot be revised;
a generation that blows its deadline halfway through a sentence has already sent
200. So a failure that happens mid-answer arrives as the last event rather than as
a status, and the client is expected to show what it has plus what went wrong. See
the ``/api/ask`` proxy on the PHP side, which makes the same argument from the
other end.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from memo_ai.ask import prompt, retrieval
from memo_ai.ask.model import Model, ModelUnavailable
from memo_ai.ask.retrieval import Source

log = logging.getLogger(__name__)

# What is said when the question had nothing to search for.
#
# Reachable from a question made entirely of stopwords -- "what about it?", "and
# then?" -- which `to_tsvector` reduces to no lexemes at all. Answered here rather
# than refused at the API edge with a 422, because it is not a malformed request: it
# is a reasonable thing to type into a box and the useful reply is a hint about what
# this box does, not a validation error.
NO_TERMS = (
    "That question has no words to search for. Try naming something you might have "
    "said -- a person, a place, a thing you were going to do."
)

# What is said when nothing matched.
#
# The distinction from NO_TERMS is worth the second sentence: one means "ask
# differently", the other means "you never recorded that", and a single message
# covering both would send somebody rephrasing a question that was already fine.
NO_MATCHES = "None of your memos mention that."


@dataclass(frozen=True)
class Event:
    """
    One thing to tell the client. A dict on the wire; a class here so the app has
    nothing to decide and the tests have something to assert against by name.
    """

    type: str
    payload: dict


def answer(
    connection: psycopg.Connection,
    model: Model,
    question: str,
    *,
    owner_id: str,
    top_k: int,
    memo_chars: int,
) -> Iterator[Event]:
    """
    Answer one question over the memos, streaming.

    The model is not called at all when there is no evidence, and that is a
    latency decision as much as an honesty one. A 1.5B model given no memos and
    asked to say it has nothing will spend twenty seconds producing a paragraph
    that says it in more words -- and might not say it, because a model asked a
    question tends to answer it. Two fixed sentences are both faster and more
    reliable than a generated apology.
    """
    found = retrieval.retrieve(
        connection, question, owner_id=owner_id, top_k=top_k, memo_chars=memo_chars
    )

    yield Event("sources", {"sources": [_source(source) for source in found.sources]})

    if not found.sources:
        yield Event("token", {"text": NO_MATCHES if found.has_terms else NO_TERMS})
        yield Event("done", {"cited": []})

        return

    # Accumulated so `cited` can be computed from the whole answer rather than
    # guessed at per token. A citation can be split across two chunks -- "[" and
    # "1]" are two tokens often enough to matter -- so matching on the pieces as
    # they go past would miss them.
    written: list[str] = []

    try:
        for text in model.stream(prompt.messages(question, found.sources)):
            written.append(text)

            yield Event("token", {"text": text})
    except ModelUnavailable as error:
        # The sentence, not the exception's type: ModelUnavailable's messages are
        # written for a person and this one goes to the browser verbatim.
        yield Event("error", {"message": str(error)})

        return

    refs = [source.ref for source in prompt.cited("".join(written), found.sources)]

    log.info("answered in %d chunk(s), citing %s", len(written), refs or "nothing")

    yield Event("done", {"cited": refs})


def _source(source: Source) -> dict:
    """One retrieved memo, as the browser is told about it."""
    return {
        "ref": source.ref,
        "id": str(source.id),
        "title": source.title,
        "created_at": _iso_z(source.created_at),
        "excerpt": source.excerpt,
        "truncated": source.truncated,
    }


def _iso_z(value: datetime) -> str:
    """
    One timestamp, in the spelling the rest of this app's wire format uses.

    Not ``isoformat()``, which renders the offset as ``+00:00`` and the fraction at
    whatever precision the value happens to carry. ``MemoRepository::COLUMNS`` puts
    milliseconds and a literal ``Z`` on every timestamp the API sends, and these
    sources reach the same client through the same proxy -- two spellings of UTC in
    one app is the kind of difference that costs somebody an afternoon.

    ``astimezone(UTC)`` rather than assuming the value is already in it. It is --
    ``created_at`` is a ``timestamptz`` and psycopg hands back an aware datetime --
    but the conversion is free and the alternative is a Z on a local time.
    """
    utc = value.astimezone(UTC)

    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"

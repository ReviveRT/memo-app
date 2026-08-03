"""
Ask, answered by Groq instead of by a model in this container.

The reason this module exists is a memory budget rather than a preference.
``memo_ai/ask/model.py`` loads Qwen2.5-1.5B-Instruct Q4 -- 1,117 MB of weights plus
llama.cpp's KV cache on top -- which is about three times what a free hosted tier
gives a whole service. Ask was the one feature ``deploy/`` shipped without, and
answering "not on a free tier" for it while transcription had already moved to a
hosted provider was a gap in the same seam rather than a law of nature.

**The contract is ``model.Model``'s, deliberately unchanged.** ``ask/service.py``
takes something with ``stream()``, ``start_loading()`` and ``state``; it neither
knows nor asks which of these it has. So this file adds a peer, not a layer:

  * ``start_loading()`` is a no-op. There is nothing to load, which is the whole
    point -- ai-api on this backend answers its first question immediately instead
    of after a 1.1 GB mmap and a warm-up.
  * ``state`` is ``ready`` or ``unconfigured``. It can never be ``loading`` or
    ``failed``, because neither state has a meaning without a local file.
  * ``stream()`` yields the same chunks of text and raises the same
    :class:`~memo_ai.ask.model.ModelUnavailable` with the same kind of sentence.

**No dependency comes with it.** ``urllib`` and ``json``, for the reason
``memo_ai/stt/groq.py`` states at length and ``memo_ai/blobs.py`` repeats: this
package installs into an image that deliberately carries neither ``requests`` nor
``httpx``, and ``ai/requirements-hosted.txt`` is one line because of decisions like
this one.

**One local constraint that does not survive the move**, and it is worth naming
because it is an improvement rather than a compromise: ``model.Model`` serialises
questions behind a lock, since two generations on four shared CPU threads make both
slow and neither correct. Groq has no such coupling -- concurrency is its problem,
not this container's -- so there is no busy state here and two people can ask at
once. That is the only behavioural difference between the two backends that a user
could notice, other than the answer arriving in about a second.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from memo_ai.ask.model import UNAVAILABLE, ModelUnavailable

# Imported rather than copied, and that is the careful choice of the two.
#
# This header is a workaround for Cloudflare in front of Groq's API refusing
# `urllib`'s default agent with `error code: 1010` -- measured against the live API,
# and documented at the constant. A workaround with two copies is a workaround that
# gets fixed in one of them; the coupling between two feature packages is the
# smaller cost, because what has to stay true is that both requests to the same
# vendor identify themselves the same way.
from memo_ai.stt.groq import USER_AGENT

log = logging.getLogger(__name__)

# OpenAI-compatible, which is the only reason this module is short: `prompt.messages`
# already returns `[{"role": ..., "content": ...}]` for llama.cpp's chat wrapper, and
# that is byte-for-byte what this endpoint wants. No translation layer, no second
# prompt format to keep in step with the first.
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Matched to `model.MAX_OUTPUT_TOKENS` and `model.TEMPERATURE` rather than chosen
# again here. The answers should read the same on both backends -- same length
# ceiling, same determinism -- and two numbers that mean "the same thing" are two
# numbers that drift. Imported at use rather than at module scope only because
# `model` imports nothing from here and the cycle is not worth risking.
#
# 0 temperature for the reason model.py gives: an answer quoting the user's own
# memos is the one place sampling buys nothing and costs reproducibility.

# How long one request may take at the socket. Separate from the deadline below and
# doing a different job: this bounds a connection that stalls with no bytes moving,
# where the deadline bounds a stream that is moving but not finishing.
#
# 30s rather than the 120s memo_ai/stt/groq.py allows itself, because the two
# requests are not comparable -- that one uploads audio and waits for a whole
# transcription, this one asks for the first token of a 320-token answer. A chat
# completion that has sent nothing in 30 seconds is not slow, it is broken.
CONNECT_TIMEOUT_SECONDS = 30.0

# Every sentence below can reach the browser verbatim through /api/ask, so the rule
# ModelUnavailable states applies unchanged: something a person can act on, plain
# ASCII, and nothing about keys, URLs or internals. In particular none of them carry
# the response body, which is a third party's prose rather than this project's.
#
# The no-key sentence is not among them: it lives in `model.UNAVAILABLE` because
# /ask refuses with it before the first byte and this module raises it after, and
# those two must be the same sentence. See the comment at that mapping.
_REJECTED = (
    "Groq rejected the configured API key, so Ask cannot answer. Check GROQ_API_KEY."
)

_RATE_LIMITED = (
    "Ask has hit the rate limit on this deployment's Groq plan. Try again in a "
    "moment."
)

_TOO_SLOW = "Generating an answer took too long and was stopped."

_FAILED = "Ask could not reach the service that answers questions. Try again."

# The state `state` reports when there is no key. A fourth key in
# `model.UNAVAILABLE`, added there rather than handled here, so that /ask's refusal
# before the first byte and stream()'s refusal after it keep coming from one mapping
# -- which is the property that comment in model.py asks for.
UNCONFIGURED = "unconfigured"


class HostedModel:
    """Groq's chat completions, streamed. A peer of :class:`~memo_ai.ask.model.Model`."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        deadline_seconds: float,
        opener: object | None = None,
    ) -> None:
        self._api_key = api_key or None
        self.model = model
        self.deadline_seconds = deadline_seconds
        # Injected for the tests, exactly as `LlmLoader` is in model.py and for the
        # same reason: every path here -- rejected key, rate limit, a stream that
        # stops mid-answer, a malformed chunk -- has to be reachable without a key
        # and without leaving the machine.
        self._opener = opener or urllib.request.urlopen

    def start_loading(self) -> None:
        """
        Nothing to load. Kept because the startup hook calls it on whichever backend
        is configured, and a backend that answered ``AttributeError`` here would make
        the seam a lie.
        """

    @property
    def state(self) -> str:
        """``ready``, or ``unconfigured`` when there is no key."""
        return "ready" if self._api_key else UNCONFIGURED

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """
        Yield the answer as Groq produces it, or raise :class:`ModelUnavailable`.

        The key is re-checked here rather than trusted from ``state``. ``/ask``
        checks it before the response begins, but the two are separate instants and
        model.py makes the same argument about the same window -- a configuration
        that changed in between should produce the sentence, not a traceback.
        """
        if not self._api_key:
            raise ModelUnavailable(UNAVAILABLE[UNCONFIGURED])

        # Imported here rather than at module scope: see the note by
        # CONNECT_TIMEOUT_SECONDS. The values belong to model.py.
        from memo_ai.ask.model import MAX_OUTPUT_TOKENS, TEMPERATURE

        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "temperature": TEMPERATURE,
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
        ).encode()

        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                # See USER_AGENT. Without it Cloudflare answers 403 and Groq never
                # sees the request.
                "User-Agent": USER_AGENT,
            },
        )

        started = time.monotonic()

        try:
            response = self._opener(request, timeout=CONNECT_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            raise ModelUnavailable(_status_message(error.code)) from error
        except (urllib.error.URLError, TimeoutError) as error:
            log.warning("ask: groq unreachable: %s", error)

            raise ModelUnavailable(_FAILED) from error

        # `with` on the response rather than around the whole call, so that a failure
        # opening it is classified above instead of falling into the generic branch.
        with response:
            yield from self._chunks(response, started)

    def _chunks(self, response: object, started: float) -> Iterator[str]:
        """
        The server-sent-event stream, as plain text chunks.

        Line-oriented rather than parsed with an SSE library, because the shape this
        endpoint actually sends is small and fixed: ``data: {json}`` lines, blank
        lines between them, and a final ``data: [DONE]``. Anything else is skipped
        rather than raised on -- a comment line or a keep-alive is not an error, and
        an answer half-delivered is worth more to the reader than an exception about
        a line that carried no content.
        """
        for raw in response:
            if self.deadline_seconds and time.monotonic() - started > self.deadline_seconds:
                log.warning(
                    "ask: abandoning an answer after %.0fs", self.deadline_seconds
                )

                raise ModelUnavailable(_TOO_SLOW)

            line = raw.decode("utf-8", "replace").strip()

            if not line.startswith("data:"):
                continue

            data = line[len("data:") :].strip()

            if data == "[DONE]":
                return

            text = _content(data)

            if text:
                yield text

    def __repr__(self) -> str:
        """Named without the key, so a log line or a traceback cannot carry it."""
        return f"HostedModel(model={self.model!r}, configured={bool(self._api_key)})"


def _content(data: str) -> str:
    """
    The text out of one ``data:`` payload, or ``""`` if it carries none.

    Every lookup is defensive on purpose. The first chunk of an OpenAI-shaped stream
    carries a role and no content, the last carries a finish reason and no content,
    and both are normal -- so `.get` chains rather than indexing, which would mean a
    KeyError on the two chunks that always occur. ``memo_ai/ask/model.py``'s
    ``_text`` makes the same choice against llama.cpp's chunks for the same reason.
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        log.warning("ask: skipping a chunk that was not json")

        return ""

    choices = payload.get("choices") or []

    if not choices:
        return ""

    delta = choices[0].get("delta") or {}
    text = delta.get("content")

    return text if isinstance(text, str) else ""


def _status_message(code: int) -> str:
    """
    An HTTP status from Groq, as a sentence for whoever asked the question.

    The three that are worth telling apart are the three a deployment actually hits:
    a key that is wrong, a plan whose limit has been reached, and everything else.
    memo_ai/stt/groq.py separates the same three on the same grounds -- 401 and 403
    were collapsed there once and a Cloudflare block then read as a bad key, which
    is the mistake this function is shaped to avoid repeating.
    """
    if code in (401, 403):
        return _REJECTED

    if code == 429:
        return _RATE_LIMITED

    log.warning("ask: groq answered %d", code)

    return _FAILED

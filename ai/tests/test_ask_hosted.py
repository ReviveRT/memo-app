"""
The hosted Ask backend, against a stubbed transport.

Every path here is reachable without a key and without leaving the machine, which is
the reason ``HostedModel`` takes an ``opener``. What this file cannot prove is the one
thing ``memo_ai/stt/groq.py`` learned the hard way -- that the request survives the
Cloudflare edge in front of Groq -- so the User-Agent assertion below is a regression
test for a defect a stub can never reproduce, and the live check belongs beside
``tests/test_groq_live.py``.
"""

import io
import json

import pytest

from memo_ai.ask.hosted import (
    CONNECT_TIMEOUT_SECONDS,
    ENDPOINT,
    UNCONFIGURED,
    HostedModel,
)
from memo_ai.ask.model import UNAVAILABLE, ModelUnavailable
from memo_ai.stt.groq import USER_AGENT

MESSAGES = [
    {"role": "system", "content": "answer from the memos"},
    {"role": "user", "content": "what did I buy?"},
]


def sse(*chunks: str, done: bool = True) -> bytes:
    """The wire format Groq sends: `data: {json}` per line, then `data: [DONE]`."""
    lines = []

    for text in chunks:
        payload = {"choices": [{"delta": {"content": text}}]}
        lines.append(f"data: {json.dumps(payload)}")

    if done:
        lines.append("data: [DONE]")

    return ("\n".join(lines) + "\n").encode()


class StubHttp:
    """Stands in for `urllib.request.urlopen`, recording the request it was given."""

    def __init__(self, body: bytes = b"", error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout=None):
        self.request = request
        self.timeout = timeout

        if self.error is not None:
            raise self.error

        # BytesIO iterates by line and supports the context manager `stream` uses,
        # which is the whole of what this code needs from an HTTP response.
        return io.BytesIO(self.body)


def model(http: StubHttp, key: str = "gsk_test", deadline: float = 180.0) -> HostedModel:
    return HostedModel(key, "llama-3.1-8b-instant", deadline_seconds=deadline, opener=http)


def test_it_streams_the_text_out_of_the_chunks():
    http = StubHttp(sse("One ", "two ", "three."))

    assert list(model(http).stream(MESSAGES)) == ["One ", "two ", "three."]


def test_the_request_carries_the_key_the_model_and_the_prompt():
    http = StubHttp(sse("hi"))

    list(model(http).stream(MESSAGES))

    assert http.request.full_url == ENDPOINT
    assert http.request.get_header("Authorization") == "Bearer gsk_test"
    assert http.timeout == CONNECT_TIMEOUT_SECONDS

    sent = json.loads(http.request.data)

    assert sent["model"] == "llama-3.1-8b-instant"
    assert sent["messages"] == MESSAGES
    assert sent["stream"] is True


def test_the_answer_is_deterministic_and_bounded():
    """Both borrowed from model.py rather than chosen again -- see the comment there."""
    from memo_ai.ask.model import MAX_OUTPUT_TOKENS, TEMPERATURE

    http = StubHttp(sse("hi"))

    list(model(http).stream(MESSAGES))
    sent = json.loads(http.request.data)

    assert sent["temperature"] == TEMPERATURE
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS


def test_the_request_sends_an_explicit_user_agent():
    """
    The regression test for the defect a stub cannot reproduce: Cloudflare in front of
    Groq refuses `urllib`'s default agent with `error code: 1010`, so the header is
    load-bearing and its absence is invisible until a real request is made.
    """
    http = StubHttp(sse("hi"))

    list(model(http).stream(MESSAGES))

    assert http.request.get_header("User-agent") == USER_AGENT
    assert "urllib" not in http.request.get_header("User-agent")


# --- what it says when it cannot answer --------------------------------------


def test_without_a_key_it_is_unconfigured_and_never_opens_a_socket():
    http = StubHttp(sse("hi"))
    unconfigured = HostedModel(None, "m", deadline_seconds=180.0, opener=http)

    assert unconfigured.state == UNCONFIGURED

    with pytest.raises(ModelUnavailable) as raised:
        list(unconfigured.stream(MESSAGES))

    # The same sentence /ask refuses with before the first byte, from one mapping.
    assert str(raised.value) == UNAVAILABLE[UNCONFIGURED]
    assert http.request is None


def test_an_empty_key_is_the_same_absence_as_no_key():
    """`GROQ_API_KEY=` in a .env is an absence, matching memo_ai/config.py's rule 1."""
    assert HostedModel("", "m", deadline_seconds=180.0).state == UNCONFIGURED


def test_a_configured_backend_is_ready_and_loads_nothing():
    ready = HostedModel("gsk_test", "m", deadline_seconds=180.0)

    assert ready.state == "ready"
    assert ready.start_loading() is None
    assert ready.state == "ready"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "rejected the configured API key"),
        (403, "rejected the configured API key"),
        (429, "rate limit"),
        (500, "could not reach"),
    ],
)
def test_http_failures_become_sentences_a_person_can_act_on(status, expected):
    import urllib.error

    http = StubHttp(error=urllib.error.HTTPError(ENDPOINT, status, "no", {}, None))

    with pytest.raises(ModelUnavailable) as raised:
        list(model(http).stream(MESSAGES))

    assert expected in str(raised.value)


def test_no_message_carries_the_key_or_the_endpoint():
    """
    Every sentence reaches the browser through two proxies, so the rule
    ModelUnavailable states applies: nothing about keys, URLs or internals.
    """
    import urllib.error

    for status in (401, 403, 429, 500, 503):
        http = StubHttp(error=urllib.error.HTTPError(ENDPOINT, status, "no", {}, None))

        with pytest.raises(ModelUnavailable) as raised:
            list(model(http).stream(MESSAGES))

        message = str(raised.value)

        assert "gsk_test" not in message
        assert "api.groq.com" not in message
        assert message.isascii()


def test_an_unreachable_host_is_a_sentence_rather_than_a_traceback():
    import urllib.error

    http = StubHttp(error=urllib.error.URLError("no route"))

    with pytest.raises(ModelUnavailable) as raised:
        list(model(http).stream(MESSAGES))

    assert "could not reach" in str(raised.value)


def test_the_repr_does_not_carry_the_key():
    """A log line or a traceback must not be the thing that leaks it."""
    text = repr(HostedModel("gsk_secret_value", "m", deadline_seconds=1.0))

    assert "gsk_secret_value" not in text
    assert "configured=True" in text


# --- the shapes a real stream contains --------------------------------------


def test_the_role_only_and_finish_only_chunks_are_skipped():
    """
    The first chunk of an OpenAI-shaped stream carries a role and no content and the
    last carries a finish reason and no content. Both are normal, so neither may raise
    and neither may yield an empty token.
    """
    body = (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
        + sse("the answer", done=False)
        + b'\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        + b"data: [DONE]\n"
    )

    assert list(model(StubHttp(body)).stream(MESSAGES)) == ["the answer"]


def test_keep_alives_blank_lines_and_comments_are_ignored():
    body = b"\n" + b": keep-alive\n" + sse("text") + b"\n"

    assert list(model(StubHttp(body)).stream(MESSAGES)) == ["text"]


def test_a_chunk_that_is_not_json_is_skipped_rather_than_fatal():
    """An answer half-delivered is worth more to the reader than an exception."""
    body = b"data: {not json\n" + sse("still fine")

    assert list(model(StubHttp(body)).stream(MESSAGES)) == ["still fine"]


def test_a_stream_that_stops_without_done_ends_the_answer():
    """
    A truncated stream is not an error here. `service.answer` computes citations from
    what arrived, so the client keeps a partial answer rather than losing it to a
    raise on the last line.
    """
    assert list(model(StubHttp(sse("half a sen", done=False))).stream(MESSAGES)) == [
        "half a sen"
    ]


def test_the_deadline_stops_a_stream_that_never_finishes(monkeypatch):
    """
    The deadline bounds a stream that is moving but not ending -- distinct from the
    socket timeout, which bounds one that has stopped moving.
    """
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))

    with pytest.raises(ModelUnavailable) as raised:
        list(model(StubHttp(sse("a", "b", "c")), deadline=10.0).stream(MESSAGES))

    assert "too long" in str(raised.value)


def test_it_satisfies_the_backend_protocol():
    """
    Structural, not nominal: HostedModel inherits nothing from Model, and app.py holds
    whichever it was given. If a member is renamed on one and not the other, this is
    the test that says so.
    """
    from memo_ai.ask.model import Model

    for member in ("start_loading", "state", "stream"):
        assert hasattr(HostedModel("k", "m", deadline_seconds=1.0), member)
        assert hasattr(Model, member)

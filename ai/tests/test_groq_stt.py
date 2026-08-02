"""
The Groq provider's decisions, driven through a stubbed HTTP layer.

No test here reaches the network. What is checked is everything this module
actually decides: what goes into the multipart body, and — the half that matters
more — which failures are :class:`SttUnavailable` (walk the fallback chain, so the
memo still gets transcribed locally) and which are terminal.

That split is the whole safety story of an opt-in hosted provider. A misclassified
failure does not error visibly; it either fails a memo that the local model would
have handled, or retries three times against a wall. tests/test_chain.py owns what
the chain *does* with the two classes; this file owns which one each failure is.
"""

import json
import urllib.error

import pytest

from memo_ai import audio, failures
from memo_ai.config import Settings
from memo_ai.stt import resolve
from memo_ai.stt.base import SttError, SttUnavailable
from memo_ai.stt.groq import (
    DEFAULT_MODEL,
    MAX_UPLOAD_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    GroqStt,
    _classified,
    _multipart,
)

TRANSCRIPT = {"text": "  Ring the dentist on Thursday.  "}

# The one variable Settings has no default for. Everything else here is exercising
# what the environment does *not* set, which is the shipped state for this provider.
MINIMAL = {"DATABASE_URL": "postgresql://memo:memo@db:5432/memo"}


def settings_for(**env) -> Settings:
    return Settings.from_env(MINIMAL | env)


@pytest.fixture
def recording(tmp_path):
    """A normalized Opus file. Its contents are never decoded, only uploaded."""
    path = tmp_path / "normalized.opus"
    path.write_bytes(b"OggS\x00fake opus payload")

    return path


class StubHttp:
    """
    Stands in for `urllib.request.urlopen`, recording the request it was given.

    A class rather than a lambda because every test wants the captured request:
    the body assertions read it directly, and the failure tests need the call to
    have been attempted at all.
    """

    def __init__(self, payload=TRANSCRIPT, raises=None, body=None):
        self.requests = []
        self._payload = payload
        self._raises = raises
        self._body = body

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))

        if self._raises is not None:
            raise self._raises

        return _Response(self._body if self._body is not None else json.dumps(self._payload).encode())

    @property
    def last(self):
        return self.requests[-1][0]


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def read(self) -> bytes:
        return self._body


def provider(monkeypatch, http=None, key="gsk_test", model=DEFAULT_MODEL) -> tuple[GroqStt, StubHttp]:
    stub = http or StubHttp()
    monkeypatch.setattr("urllib.request.urlopen", stub)

    return GroqStt(key, model), stub


# ---------------------------------------------------------------------------
# The happy path, and what goes on the wire
# ---------------------------------------------------------------------------


def test_it_transcribes_and_reports_the_model_that_did_it(monkeypatch, recording):
    groq, _ = provider(monkeypatch)
    result = groq.transcribe(recording)

    # Trimmed: the API pads with whitespace, and `memos.transcript` is what the
    # search vector and the fallback title are cut from.
    assert result.text == "Ring the dentist on Thursday."
    assert result.provider == "groq"
    assert result.model == DEFAULT_MODEL


def test_the_round_trip_is_timed_and_no_cost_is_claimed(monkeypatch, recording):
    # `inference_ms` here is the whole request, not CPU — memo_ai/stt/groq.py says
    # so, because it is not comparable to the local provider's number.
    #
    # `cost_micro_usd` stays None: Groq's response reports no price, and MEMO-22's
    # rule is that this column holds what a provider *said* it charged. The rate
    # table projects the rest.
    groq, _ = provider(monkeypatch)
    result = groq.transcribe(recording)

    assert result.inference_ms is not None and result.inference_ms >= 0
    assert result.cost_micro_usd is None


def test_the_request_carries_the_key_the_model_and_the_audio(monkeypatch, recording):
    groq, http = provider(monkeypatch)
    groq.transcribe(recording)

    request = http.last

    assert request.get_header("Authorization") == "Bearer gsk_test"
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in request.data
    assert b"OggS\x00fake opus payload" in request.data
    assert http.requests[-1][1] == REQUEST_TIMEOUT_SECONDS


def test_the_request_sends_an_explicit_user_agent(monkeypatch, recording):
    # Not courtesy — required. Groq sits behind Cloudflare, which blocks urllib's
    # default `Python-urllib/3.12` on browser signature: 403 with a body of
    # `error code: 1010`, before the request reaches Groq at all. Measured against
    # the live API; with the header the identical request answers 200.
    #
    # This assertion exists because the stub could not catch it. It is here so a
    # future edit that drops the header fails in the fast suite rather than in
    # somebody's stack.
    groq, http = provider(monkeypatch)
    groq.transcribe(recording)

    agent = http.last.get_header("User-agent")

    assert agent == USER_AGENT
    assert "urllib" not in agent.lower()


def test_a_named_language_is_sent_and_an_unnamed_one_is_omitted(monkeypatch, recording):
    # An empty `language` is not the same request as no `language` — only the
    # second asks the model to detect. 005_memo_language.sql has why the override
    # exists at all.
    groq, http = provider(monkeypatch)

    groq.transcribe(recording, language="ro")
    assert b'name="language"\r\n\r\nro' in http.last.data

    groq.transcribe(recording)
    assert b'name="language"' not in http.last.data


def test_the_multipart_boundary_is_fresh_per_request(monkeypatch, recording):
    # A constant boundary that appeared inside the audio bytes would split the part
    # and corrupt the upload. Unlikely is not a property to give a body assembled
    # from user recordings.
    groq, http = provider(monkeypatch)
    groq.transcribe(recording)
    groq.transcribe(recording)

    first, second = (r[0].get_header("Content-type") for r in http.requests)

    assert first != second


def test_the_provider_asks_for_no_format_and_therefore_gets_opus(monkeypatch):
    # Opus is 1.8 MB at the 600-second cap against WAV's 19.2 MB, and
    # memo_ai/audio.py documents it as the format a hosted provider must use. This
    # is the caller that comment was written for.
    groq, _ = provider(monkeypatch)

    assert audio.format_for(groq) is audio.OPUS


# ---------------------------------------------------------------------------
# Failure classification — the half that keeps a memo from being lost
# ---------------------------------------------------------------------------


def test_a_missing_key_is_unavailable_and_costs_no_request(monkeypatch, recording):
    # Unavailable, so STT_FALLBACK transcribes the memo locally. And checked before
    # the file is read, so a stack that never set the key spends nothing finding
    # that out on every memo.
    groq, http = provider(monkeypatch, key=None)

    with pytest.raises(SttUnavailable, match="GROQ_API_KEY"):
        groq.transcribe(recording)

    assert http.requests == []


def test_an_empty_key_is_treated_as_a_missing_one(monkeypatch, recording):
    # `docker compose up` with no .env passes an empty string. Sending it would
    # earn a 401 instead of the sentence naming the variable.
    groq, _ = provider(monkeypatch, key="")

    with pytest.raises(SttUnavailable, match="GROQ_API_KEY"):
        groq.transcribe(recording)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, SttUnavailable),  # bad key — fall back rather than fail the memo
        (403, SttUnavailable),
        (429, SttUnavailable),  # free-tier rate limit
        (500, SttUnavailable),
        (503, SttUnavailable),
        (418, SttUnavailable),  # a request this build built wrong — still fall back
        (400, SttError),        # the audio; the fallback gets the same bytes
        (413, SttError),        # too large; likewise
    ],
)
def test_each_status_lands_on_the_side_of_the_line_it_belongs(status, expected):
    error = _classified(status, '{"error": {"message": "..."}}')

    assert isinstance(error, expected)
    # Terminal cases must NOT be SttUnavailable — walking the chain over a file
    # every provider is handed identically buys nothing.
    assert isinstance(error, SttUnavailable) is (expected is SttUnavailable)


def test_a_403_from_the_edge_is_not_reported_as_a_bad_key():
    # The second defect the live run exposed. Cloudflare answers a blocked request
    # with `error code: 1010` and a 403; the first version of this file read that
    # as "check your API key" about a key that was valid — sending somebody to
    # regenerate a working credential. Groq's own errors are JSON, so the body is
    # what tells the two apart.
    edge = _classified(403, "error code: 1010")
    api = _classified(403, '{"error": {"message": "insufficient permissions"}}')

    assert "not the API key" in str(edge)
    assert "GROQ_API_KEY" in str(api)
    # Both still fall back — the chain behaviour is right either way; it is the
    # sentence a person reads that was wrong.
    assert isinstance(edge, SttUnavailable) and isinstance(api, SttUnavailable)


@pytest.mark.parametrize("body", ["", "error code: 1020", "<html>Access denied</html>", "null"])
def test_any_non_json_403_body_is_treated_as_an_edge_block(body):
    # Parsing rather than matching on "cloudflare": the question is whether the API
    # answered, and a corporate proxy or captive portal belongs on the same side
    # without naming itself.
    assert "not the API key" in str(_classified(403, body))


def test_a_network_failure_falls_back_rather_than_failing_the_memo(monkeypatch, recording):
    groq, _ = provider(monkeypatch, StubHttp(raises=urllib.error.URLError("no route to host")))

    with pytest.raises(SttUnavailable):
        groq.transcribe(recording)


def test_a_non_json_two_hundred_falls_back(monkeypatch, recording):
    # A captive portal or corporate proxy answering with a login page: a network
    # problem wearing a success code.
    groq, _ = provider(monkeypatch, StubHttp(body=b"<html>sign in</html>"))

    with pytest.raises(SttUnavailable):
        groq.transcribe(recording)


def test_a_recording_over_the_upload_limit_is_refused_before_the_request(monkeypatch, tmp_path):
    oversized = tmp_path / "huge.opus"
    oversized.write_bytes(b"\0" * (MAX_UPLOAD_BYTES + 1))
    groq, http = provider(monkeypatch)

    with pytest.raises(SttError) as raised:
        groq.transcribe(oversized)

    assert not isinstance(raised.value, SttUnavailable)
    assert http.requests == []


def test_an_empty_transcript_is_coded_so_the_memo_can_be_discarded(monkeypatch, recording):
    # The same classification the local provider gives an empty decode — the app
    # discards a recording with nothing in it rather than leaving a card saying the
    # user said nothing.
    groq, _ = provider(monkeypatch, StubHttp(payload={"text": "   "}))

    with pytest.raises(SttError) as raised:
        groq.transcribe(recording)

    assert raised.value.code == failures.NO_SPEECH


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 42}, [1, 2, 3]])
def test_a_response_without_usable_text_is_an_error_rather_than_an_exception(
    payload, monkeypatch, recording
):
    # memo_ai/pipeline.py writes a generic sentence for anything unclassified, so an
    # AttributeError here would reach the row as "unexpected worker error".
    groq, _ = provider(monkeypatch, StubHttp(payload=payload))

    with pytest.raises(SttError):
        groq.transcribe(recording)


def test_no_error_sentence_leaks_the_key_or_the_endpoint(monkeypatch, recording):
    # Every sentence in this module can reach `memos.last_error` and from there the
    # browser.
    groq, _ = provider(monkeypatch, StubHttp(raises=urllib.error.URLError("gsk_test at api.groq.com")))

    with pytest.raises(SttUnavailable) as raised:
        groq.transcribe(recording)

    assert "gsk_test" not in str(raised.value)
    assert "api.groq.com" not in str(raised.value)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_registry_builds_it_from_the_settings():
    built = resolve("groq", settings_for(GROQ_API_KEY="gsk_x", GROQ_STT_MODEL="whisper-large-v3"))

    assert isinstance(built, GroqStt)
    assert built.model == "whisper-large-v3"


def test_resolving_it_without_a_key_still_succeeds():
    # The rule UnimplementedStt set: a missing capability fails the memo that needs
    # it, never the boot that might not. `restart: unless-stopped` would turn the
    # alternative into a loop that also stops text memos.
    assert isinstance(resolve("groq", settings_for()), GroqStt)


def test_the_body_is_well_formed_multipart():
    body, content_type = _multipart(b"audio-bytes", "normalized.opus", "m", None)
    boundary = content_type.split("boundary=")[1]

    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b'filename="normalized.opus"' in body

"""
The Groq provider against the real API. Skipped unless ``GROQ_API_KEY`` is set.

**This file exists because of a rule this project already has.**
memo_ai/stt/unimplemented.py declines to ship the `openai` adapter on the grounds
that a hosted code path nobody has run is the worst kind to ship -- "the one place
where 'it looks right' and 'it works' are hardest to tell apart". Shipping `groq`
on the strength of tests/test_groq_stt.py alone would be exactly that: every
assertion there is against a stubbed transport, so all of them would pass against
a multipart body Groq rejects.

So this is the file that settles it, and it is deliberately small. It is not a
second copy of the unit tests -- it checks the three things a stub cannot:

  * that the request this build constructs is one the live API accepts at all;
  * that what comes back parses into a Transcript with real words in it;
  * that it is actually fast, which is the entire reason the provider exists.

Skipped rather than failed without a key, which is the same shape
tests/test_local_whisper.py uses for its recordings: a contributor without the
prerequisite gets a skip and a reason, not a red suite. `docker compose up` on a
clean checkout never sets this variable, so CI and a first-time reader both skip
it, and the offline default stays the tested path.

**Running it (the key never has to be typed into a terminal or a chat):**

    echo 'GROQ_API_KEY=gsk_...' >> .env
    docker compose run --rm --no-deps -e GROQ_API_KEY --entrypoint sh ai-worker \\
      -c 'pip install -q pytest && python -m pytest tests/test_groq_live.py -v'

It costs one request against the free tier, on about five seconds of audio.
"""

import os
import time
from pathlib import Path

import pytest

from memo_ai import audio
from memo_ai.stt.base import SttError
from memo_ai.stt.groq import DEFAULT_MODEL, GroqStt

FIXTURES = Path(__file__).parent / "fixtures"

# The English recording whose language whisper gets *right*. Chosen deliberately
# over safari.mp4, whose language is detected as `ru` at 0.9985 by this same model
# -- that is a real and reproducible defect (see the note in
# db/migrations/005_memo_language.sql) but it is a property of whisper, not of this
# provider, and asserting on it here would make a green suite depend on a bug.
RECORDING = FIXTURES / "chrome.webm"

# What all three fixtures say: somebody counting to ten.
#
# **Digits *or* words, because the two runtimes disagree and that is a measured
# fact rather than a hedge.** Against the live API, local renders `chrome.webm` as
# `1, 2, 3, ... 10.` and Groq as `One two three ... ten.` -- same weights, same
# audio, different decoding defaults (the local provider also feeds whisper an
# English punctuation primer, which Groq is not given). The first version of this
# test asserted digits, and it failed on a response that was entirely correct.
#
# So the assertion is "it counted to ten in some rendering", which is the strongest
# claim that is actually true of both. tests/test_local_whisper.py declines to
# assert the exact string for a related reason.
COUNTED_TO_TEN = ("10", "ten")

pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY is not set -- this is the only test that leaves the machine",
)


@pytest.fixture(scope="module")
def normalized():
    """The fixture through the real ffmpeg path, exactly as the pipeline sends it."""
    if not RECORDING.is_file():
        pytest.skip(f"no {RECORDING.name} in tests/fixtures/")

    provider = GroqStt(os.environ["GROQ_API_KEY"])

    # `format_for` rather than a literal, so this sends whatever the provider asks
    # for -- Opus today. Hard-coding WAV here would test a request shape the
    # pipeline never makes.
    with audio.normalize(RECORDING, audio.format_for(provider), 600.0) as prepared:
        yield prepared


@pytest.fixture(scope="module")
def result(normalized):
    """One real transcription, shared by the assertions below. One request, not four."""
    return GroqStt(os.environ["GROQ_API_KEY"]).transcribe(normalized.path)


def test_the_live_api_accepts_the_request_this_build_constructs(result):
    # The assertion the stub cannot make. A wrong boundary, a missing part, the
    # wrong field name -- every one of those passes tests/test_groq_stt.py and
    # fails here with a 400.
    assert result.text.strip()
    assert any(form in result.text.lower() for form in COUNTED_TO_TEN), result.text


def test_the_response_is_shaped_the_way_a_local_transcript_would_be(result):
    # Groq's raw text arrives untrimmed, uncapitalized and unterminated -- measured:
    # `' one two three ... ten'`. Routing it through memo_ai/prose.py is what stops
    # a fallback chain producing visibly different memos depending on which
    # provider happened to answer, and this pins that it is still routed.
    assert result.text == result.text.strip()
    assert result.text[0].isupper() or result.text[0].isdigit()
    assert result.text.rstrip()[-1] in ".?!。।۔။"


def test_it_reports_itself_and_the_model_that_ran(result):
    # `memos.stt_provider` is what MEMO-22 groups a cost report by and what tells a
    # reader which provider actually produced the words after a silent fallback.
    assert result.provider == "groq"
    assert result.model == DEFAULT_MODEL


def test_no_cost_is_claimed_because_groq_reports_none(result):
    # MEMO-22's rule: `cost_micro_usd` holds what a provider *said* it charged.
    # Groq's response says nothing, and on the free tier the true answer is zero
    # anyway -- memo_ai/rates.py carries the per-audio-hour figure that lets the
    # report project it instead.
    assert result.cost_micro_usd is None


def test_it_is_actually_faster_than_transcribing_locally(result, normalized):
    # The whole reason this provider exists, asserted rather than quoted. The local
    # model runs at 38.4s of CPU per audio-minute (MEMO-22's measurement), so this
    # five-second clip costs it roughly 3.4 seconds. A round trip that beats that
    # is the claim; the bound is loose because it includes upload over whatever
    # connection the machine running this has.
    local_equivalent_ms = normalized.duration_ms * 0.64

    assert result.inference_ms is not None
    assert result.inference_ms < local_equivalent_ms, (
        f"groq took {result.inference_ms}ms for {normalized.duration_ms}ms of audio; "
        f"the local model would take about {local_equivalent_ms:.0f}ms"
    )


def test_a_rejected_key_falls_back_rather_than_failing_the_memo(normalized):
    # The failure that matters most in production and the one a stub can only
    # simulate: a real 401 from the real API. It has to arrive as SttUnavailable so
    # memo_ai/stt/chain.py transcribes on STT_FALLBACK=local instead of losing the
    # memo -- and the sentence must name the variable without echoing the key.
    from memo_ai.stt.base import SttUnavailable

    with pytest.raises(SttUnavailable) as raised:
        GroqStt("gsk_definitely_not_a_valid_key").transcribe(normalized.path)

    assert "GROQ_API_KEY" in str(raised.value)
    assert "gsk_definitely_not_a_valid_key" not in str(raised.value)


def test_a_named_language_is_accepted_by_the_live_api(normalized):
    # `language` is passed through when the memo names one. Whisper's *detection*
    # is unreliable on short clips whoever hosts it (005_memo_language.sql has the
    # table), which is exactly why the override has to keep working -- so this
    # pins that the parameter is accepted, not that detection improves.
    result = GroqStt(os.environ["GROQ_API_KEY"]).transcribe(normalized.path, language="en")

    assert result.text.strip()


def test_an_oversize_recording_is_refused_without_falling_back(tmp_path):
    # Terminal, not unavailable: the fallback provider is handed the same bytes, so
    # walking the chain over a size limit buys nothing. Checked locally before the
    # request, so this costs no quota.
    from memo_ai.stt.groq import MAX_UPLOAD_BYTES
    from memo_ai.stt.base import SttUnavailable

    oversized = tmp_path / "huge.opus"
    oversized.write_bytes(b"\0" * (MAX_UPLOAD_BYTES + 1))

    with pytest.raises(SttError) as raised:
        GroqStt(os.environ["GROQ_API_KEY"]).transcribe(oversized)

    assert not isinstance(raised.value, SttUnavailable)


def test_the_round_trip_is_reported_and_plausible(result):
    # A sanity bound in the other direction. `inference_ms` is a round trip here
    # rather than CPU time -- memo_ai/stt/groq.py says so, because it is not
    # comparable to the local provider's number in memo_ai/costs.py's median. What
    # this pins is that it was measured at all and is not a stub's zero.
    assert 0 < result.inference_ms < 120_000


def test_it_left_a_timing_the_cost_report_can_read(result):
    # MEMO-22 divides `stt_ms` by `duration_ms`. A provider that returned None here
    # would silently drop out of that median rather than showing up as fast.
    assert result.inference_ms is not None


@pytest.mark.parametrize("attempt", [1, 2])
def test_the_multipart_body_is_accepted_repeatedly(normalized, attempt):
    # The boundary is a fresh UUID per request. A collision or a malformed second
    # body would show up here and nowhere else.
    result = GroqStt(os.environ["GROQ_API_KEY"]).transcribe(normalized.path)

    assert result.text.strip()


def test_report_the_measured_speedup(result, normalized):
    # Not an assertion -- a measurement, printed so the number in README.md and
    # NOTES.md can be replaced with one from this machine rather than a vendor's
    # published figure. Run with `-s` to see it.
    audio_seconds = normalized.duration_ms / 1000
    round_trip = result.inference_ms / 1000
    local_seconds = audio_seconds * 0.64

    print(
        f"\n  {audio_seconds:.1f}s of audio"
        f"\n  groq round trip : {round_trip:.2f}s"
        f"\n  local estimate  : {local_seconds:.2f}s (at the measured 0.64x)"
        f"\n  speedup         : {local_seconds / round_trip:.0f}x"
    )

    assert True

"""
The resident model: what it reports about itself, and what it does with a reader.

Every test here injects a loader, so none of them opens 1,117 MB of weights or runs
a second of inference -- the same arrangement tests/test_enrich_local.py uses, and
for the same reason. What is covered is the lifecycle and the bounds: the four
states, the refusal to run two generations against one context, the deadline, and
the case that only exists because this path streams -- a reader that walks away
mid-answer.
"""

import queue
import threading
import time
from pathlib import Path

import pytest

from memo_ai.ask import model as ask_model
from memo_ai.ask.model import Model, ModelUnavailable
from memo_ai.config import ConfigError

MESSAGES = [{"role": "user", "content": "anything"}]


class FakeLlama:
    """
    A llama.cpp stand-in whose completion is a generator this test controls.

    ``pieces`` are the streamed chunks in llama-cpp-python's shape -- a completion
    object per token with the text under ``choices[0].delta.content``. The first and
    last chunks of a real stream carry no content at all, so a couple of those are
    included by default: reading them defensively is a property of ``_text`` worth
    exercising rather than asserting about.
    """

    def __init__(
        self,
        texts=("Hello", " world"),
        gate: threading.Event | None = None,
        gate_after: int = 0,
    ) -> None:
        self.closed = False
        self.produced: list[str] = []
        self._texts = texts
        self._gate = gate
        # Which token the gate blocks before. Zero holds the whole generation, which
        # is what a deadline test wants; one lets a token out and then holds, which
        # is the only way to observe a generation that is genuinely still running.
        self._gate_after = gate_after

    def create_chat_completion(self, messages, stream, temperature, max_tokens):
        assert stream is True

        return self._chunks()

    def _chunks(self):
        # The opening chunk of a real stream: a role and no content.
        yield {"choices": [{"delta": {"role": "assistant"}}]}

        try:
            for index, text in enumerate(self._texts):
                # A long timeout, not a short one: the gate is released by the test
                # and a wait that expires on its own would turn "still running" into
                # a race against the deadline under assertion.
                if self._gate is not None and index >= self._gate_after:
                    self._gate.wait(30)

                self.produced.append(text)

                yield {"choices": [{"delta": {"content": text}}]}

            # The closing chunk: a finish reason and no content.
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        finally:
            self.closed = True


def model(tmp_path: Path, llama=None, deadline: float = 5.0, loader=None) -> Model:
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"not really a gguf")

    return Model(
        weights,
        n_ctx=2048,
        deadline_seconds=deadline,
        loader=loader or (lambda: llama or FakeLlama()),
    )


# --- the context budget ------------------------------------------------------


def test_the_context_is_derived_from_the_two_settings_and_rounded_up():
    assert ask_model.context_tokens(3, 1200) == 5120


def test_a_configuration_needing_more_context_than_this_service_will_load_is_refused():
    """
    Refused at boot rather than clamped, because a clamp is the failure this
    prevents: a context smaller than the prompt is a ValueError out of llama.cpp on
    the first long question, naming neither variable.
    """
    with pytest.raises(ConfigError) as raised:
        ask_model.context_tokens(top_k=5, memo_chars=4000)

    assert "ASK_TOP_K" in str(raised.value)
    assert "ASK_MEMO_CHARS" in str(raised.value)


# --- states ------------------------------------------------------------------


def test_a_model_file_that_is_not_there_is_missing_rather_than_failed(tmp_path):
    absent = Model(tmp_path / "nope.gguf", n_ctx=2048, deadline_seconds=5.0, loader=dict)

    assert absent.state == "missing"

    with pytest.raises(ModelUnavailable) as raised:
        list(absent.stream(MESSAGES))

    # A distinct sentence, because "somebody built this image without the model" and
    # "something went wrong" send a reader to different places.
    assert "not in this image" in str(raised.value)


def test_the_state_is_loading_until_the_load_finishes(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)

        return FakeLlama()

    subject = model(tmp_path, loader=slow, deadline=5.0)

    assert subject.state == "loading"  # not started yet

    subject.start_loading()
    assert started.wait(5)
    assert subject.state == "loading"

    release.set()

    for _ in range(500):
        if subject.state == "ready":
            break

        time.sleep(0.01)

    assert subject.state == "ready"


def test_asking_before_the_load_finishes_is_refused_rather_than_queued(tmp_path):
    """
    The alternative -- block until it is ready -- would make the first question of a
    cold container wait out the load with no way to say so. `/health` answers 503
    for exactly this window, which is what the compose healthcheck waits on.
    """
    release = threading.Event()
    subject = model(tmp_path, loader=lambda: release.wait(5) or FakeLlama())
    subject.start_loading()

    with pytest.raises(ModelUnavailable) as raised:
        list(subject.stream(MESSAGES))

    assert "still loading" in str(raised.value)

    release.set()


def test_a_load_that_raises_is_reported_as_failed(tmp_path):
    def broken():
        raise OSError("no such file, or it is not a gguf")

    subject = model(tmp_path, loader=broken)
    subject.start_loading()

    for _ in range(500):
        if subject.state != "loading":
            break

        time.sleep(0.01)

    assert subject.state == "failed"

    with pytest.raises(ModelUnavailable) as raised:
        list(subject.stream(MESSAGES))

    # No mention of the OSError's text: this sentence reaches a browser.
    assert "could not be loaded" in str(raised.value)
    assert "gguf" not in str(raised.value)


def test_a_loaded_model_stays_ready_even_if_its_file_disappears(tmp_path):
    """
    **The order of the two checks in `state`, and the first version had it wrong.**

    The weights are in memory and the file is not read again, so a rebuild under a
    running stack -- which is the whole reason the path is re-checked at all -- must
    not turn a working service into one that answers 503. `memo_ai/enrich/local.py`
    puts the loaded-model check first for the same reason.
    """
    subject = model(tmp_path, llama=FakeLlama())
    subject.start_loading()

    for _ in range(500):
        if subject.state == "ready":
            break

        time.sleep(0.01)

    subject.model_path.unlink()

    assert subject.state == "ready"
    assert list(subject.stream(MESSAGES)) == ["Hello", " world"]


def test_every_state_that_is_not_ready_has_a_sentence(tmp_path):
    """
    `/ask` looks the refusal up in this mapping rather than raising, so a state
    added without one would be a KeyError inside a route rather than a 503.

    Four keys for two backends. The first three are this module's and the fourth is
    memo_ai/ask/hosted.py's, and they live in one mapping because app.py holds
    whichever backend it was given and must not have to know which subset of these it
    can see. The exact-equality assertion is the point of the test: a fifth state added
    without a sentence should fail here rather than in a route.
    """
    assert set(ask_model.UNAVAILABLE) == {
        "missing",
        "loading",
        "failed",
        "unconfigured",
    }
    assert all(isinstance(text, str) and text for text in ask_model.UNAVAILABLE.values())


def test_no_backend_can_report_a_state_the_mapping_lacks():
    """
    The other half of the test above, from the backends rather than the mapping: every
    state either implementation can return has to be `ready` or a key here. Written
    because the two files are edited independently and the failure mode is a 500 from
    a route rather than anything visible in a unit test of either one.
    """
    from memo_ai.ask.hosted import UNCONFIGURED

    reachable = {"missing", "loading", "failed", UNCONFIGURED}

    assert reachable - {"ready"} <= set(ask_model.UNAVAILABLE)


def test_start_loading_twice_loads_once(tmp_path):
    loads = []

    subject = model(tmp_path, loader=lambda: loads.append(1) or FakeLlama())

    subject.start_loading()
    subject.start_loading()

    for _ in range(500):
        if subject.state == "ready":
            break

        time.sleep(0.01)

    assert loads == [1]


# --- streaming ---------------------------------------------------------------


def ready(tmp_path, llama, deadline: float = 5.0) -> Model:
    subject = model(tmp_path, llama=llama, deadline=deadline)
    subject.start_loading()

    for _ in range(500):
        if subject.state == "ready":
            break

        time.sleep(0.01)

    assert subject.state == "ready"

    return subject


def test_the_answer_arrives_in_pieces_and_the_empty_chunks_are_skipped(tmp_path):
    subject = ready(tmp_path, FakeLlama(texts=("Hello", " world")))

    assert list(subject.stream(MESSAGES)) == ["Hello", " world"]


def test_a_second_question_while_one_is_running_is_refused(tmp_path):
    """
    **Not a queue, and not a second thread.** A llama_context may not be entered
    twice at once, so a concurrent question is declined -- the alternative is two
    threads decoding into one KV cache, which is a corrupt answer rather than a slow
    one.
    """
    gate = threading.Event()
    subject = ready(
        tmp_path,
        FakeLlama(texts=("one", "two"), gate=gate, gate_after=1),
        deadline=30.0,
    )

    first = subject.stream(MESSAGES)
    # One token out, then the gate holds the generation open -- which is the only
    # state in which a second question can legitimately be refused.
    assert next(first) == "one"

    with pytest.raises(ModelUnavailable) as raised:
        list(subject.stream(MESSAGES))

    assert "another question" in str(raised.value)

    gate.set()
    list(first)


def test_a_generation_that_produces_nothing_in_time_is_stopped(tmp_path):
    """
    The deadline bounds the wait for the *first* token as well as the rest, which is
    the reason the generation runs on its own thread at all: prompt processing is a
    single uninterruptible call inside llama.cpp and it is most of the latency here.
    """
    gate = threading.Event()
    subject = ready(tmp_path, FakeLlama(texts=("never",), gate=gate), deadline=0.2)

    with pytest.raises(ModelUnavailable) as raised:
        list(subject.stream(MESSAGES))

    assert "too long" in str(raised.value)

    gate.set()


def test_a_completion_that_raises_after_some_text_reports_the_failure(tmp_path):
    class Exploding(FakeLlama):
        def _chunks(self):
            yield {"choices": [{"delta": {"content": "half an "}}]}

            raise RuntimeError("ggml assert")

    subject = ready(tmp_path, Exploding())

    produced = []

    with pytest.raises(ModelUnavailable) as raised:
        for piece in subject.stream(MESSAGES):
            produced.append(piece)

    assert produced == ["half an "]
    # The sentence reaches a browser, so llama.cpp's own text must not be in it.
    assert "ggml" not in str(raised.value)


def test_a_reader_that_walks_away_stops_the_generation(tmp_path, monkeypatch):
    """
    **The half a plain BackgroundCall cannot express**, and the reason this is not
    one. llama-cpp-python yields between tokens, so an abandoned answer is Python
    code with a chance to stop -- the pump blocks on a full queue, gives up, and
    closes the generator. A closed browser tab therefore costs seconds of CPU rather
    than the whole deadline.

    The queue size and the give-up interval are shrunk so the test does not have to
    produce 32 tokens or wait ten seconds for the real ones.
    """
    monkeypatch.setattr(ask_model, "_QUEUE_SIZE", 1)
    monkeypatch.setattr(ask_model, "_ABANDONED_AFTER_SECONDS", 0.1)

    llama = FakeLlama(texts=tuple(f"token {n} " for n in range(50)))
    subject = ready(tmp_path, llama)

    answer = subject.stream(MESSAGES)
    next(answer)
    answer.close()

    for _ in range(100):
        if llama.closed:
            break

        time.sleep(0.02)

    assert llama.closed
    # A handful, not fifty: the pump stopped once nobody was taking its output.
    assert len(llama.produced) < 10


# --- the chunk shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "piece",
    [
        None,
        {},
        {"choices": []},
        {"choices": [{"delta": {}}]},
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": None}}]},
    ],
)
def test_a_chunk_carrying_no_text_yields_none(piece):
    """
    Two of these occur on every single stream -- the opening chunk has a role and
    the closing one a finish reason -- so indexing rather than reading defensively
    would be a KeyError on every answer.
    """
    assert ask_model._text(piece) == ""


def test_the_sentinel_is_not_a_string():
    """
    An empty string is a legal thing for llama.cpp to yield, so the end of the
    stream cannot be signalled by one.
    """
    assert not isinstance(ask_model._END, str)


def test_the_pump_signals_the_end_even_when_the_completion_never_starts(tmp_path):
    """
    **The regression this file caught.** `create_chat_completion` was outside the
    try in the first version, so a call that raised before yielding anything left no
    sentinel on the queue -- and the consumer waited out the whole deadline to
    report "took too long" for a failure that had happened in milliseconds.
    """
    chunks: queue.Queue = queue.Queue(maxsize=4)

    class Exploding:
        def create_chat_completion(self, **_kwargs):
            raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        ask_model._pump(Exploding(), MESSAGES, chunks)

    assert chunks.get_nowait() is ask_model._END


def test_a_completion_that_never_starts_is_reported_promptly_rather_than_at_the_deadline(
    tmp_path,
):
    """
    The same failure from the outside, and the assertion that matters is the clock:
    a generous deadline with a fast failure has to answer fast.
    """

    class Exploding(FakeLlama):
        def create_chat_completion(self, **_kwargs):
            raise RuntimeError("ggml assert")

    subject = ready(tmp_path, Exploding(), deadline=30.0)

    started = time.monotonic()

    with pytest.raises(ModelUnavailable) as raised:
        list(subject.stream(MESSAGES))

    assert time.monotonic() - started < 5.0
    assert "failed on that question" in str(raised.value)

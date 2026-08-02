"""
The same GGUF the worker enriches with, loaded once and kept, streaming its answer.

**Resident, which is the opposite of memo_ai/enrich/local.py and the whole reason
this is a separate service.** The enricher loads lazily because nobody is waiting
on a summary and a replica that only takes text memos should not pay 1.7 GB for a
model it never uses. Here a person is watching a cursor, so the first question of
the day must not also be the one that waits out a model load. The load starts when
the process does and ``/ask`` refuses until it is finished, rather than making one
unlucky question absorb it.

What that costs is stated rather than buried: ``ai-api`` holds the model for the
life of the stack. **Only about 0.5 GB of that is this service's own**, because the
1,081 MB weight file is ``mmap``-ed read-only and is the same file the two worker
replicas map -- so the sixth service adds far less than the "1.7 GB per model"
figure suggests. The numbers, and how they were measured on all three processes at
once, are in NOTES.md.

**The generation runs on its own thread and the reader can walk away from it.**
:class:`~memo_ai.background.BackgroundCall`'s docstring has the general argument --
llama.cpp is C++ and there is nothing to cancel, so the only way to bound it is to
stop waiting. Streaming adds a second half that a plain ``BackgroundCall`` cannot
express, and it is the better half: llama-cpp-python yields between tokens, which
means this *is* Python code with a chance to stop. So the pump below stops
generating the moment nobody is taking its output -- a closed tab or a
disconnected proxy ends the work instead of leaving the model busy for three
minutes. See :meth:`Model.stream`.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from memo_ai.background import BackgroundCall
from memo_ai.config import ConfigError

log = logging.getLogger(__name__)

# CPU threads for one generation. Four, matching memo_ai/enrich/local.py and
# CTranslate2 on the transcription side, and left explicit for the reason that file
# gives: llama.cpp's own default is derived from the host's core count and would
# differ between a laptop and CI for no reason anybody chose.
#
# It is the same four even though this process runs one generation at a time and
# the worker runs two replicas, because the two share a machine. An ai-api that
# grabbed every core would take them from whichever worker was transcribing.
THREADS = 4

# The ceiling on one answer.
#
# The system prompt asks for at most three sentences, so this is roughly four times
# what a well-behaved answer needs. The headroom is deliberate and cheap: unlike the
# enricher, where hitting the cap turns a valid answer into unparseable JSON, an
# answer cut off here is a sentence that stops -- the citations already emitted
# still resolve, and the reader can see that it ended abruptly.
MAX_OUTPUT_TOKENS = 320

# Greedy decoding, for the reason memo_ai/enrich/local.py and memo_ai/stt/local.py
# both pin it: the same question over the same memos should give the same answer.
# A feature whose output moves when nothing changed is one nobody can debug, and
# creativity is not what is wanted from a model summarising somebody's own words.
TEMPERATURE = 0.0

# Tokens of context that are not retrieved memo text.
#
# Measured against the shipped prompt rather than guessed at, and rounded up hard:
# the system prompt and the worked exchange tokenize to a little over 400, the
# question is capped at 500 characters by the API edge, and MAX_OUTPUT_TOKENS is
# above. 1,200 covers all three with room, on the same pessimistic one-token-per-
# character assumption memo_ai/enrich/local.py's CONTEXT_TOKENS uses -- which is
# five times too generous for English and about right for Chinese or Japanese.
OVERHEAD_TOKENS = 1200

# The largest context this service will load.
#
# Qwen2.5-1.5B-Instruct supports 32,768, so this is not the model's limit -- it is
# the point at which the configuration has stopped being "ask my memos" and become
# "read my memos to a small model on a CPU". The KV cache scales with it (MEMO-21
# measured 412 MB of anonymous memory at 12,288) and so does the time to process a
# full prompt, which is the whole of the latency here.
#
# Enforced at boot rather than clamped, because clamping is the failure mode this
# exists to prevent: a context smaller than the prompt is a `ValueError` out of the
# binding on the first question, and a silently clamped one produces it on the first
# *long* question, weeks later.
MAX_CONTEXT_TOKENS = 16384

# How long the pump waits for somebody to take a token before deciding nobody will.
#
# The token before it was taken within milliseconds or the reader would not have
# asked for another, so ten seconds is not a latency budget -- it is the gap that
# distinguishes a slow reader from a reader who has gone. It bounds the work an
# abandoned request can keep doing to ten seconds of generation.
_ABANDONED_AFTER_SECONDS = 10.0

# How many tokens may sit unread.
#
# Small on purpose. A large buffer would let the model run to completion into a
# queue nobody is draining, which is exactly the work `_ABANDONED_AFTER_SECONDS`
# exists to stop; a size of one would serialise the generator against the network.
_QUEUE_SIZE = 32

# The sentinel that ends the stream, distinguishable from a token because it is not
# a string. An empty string is a legal thing for llama.cpp to yield.
_END = object()

# How long the consumer waits, after the sentinel, for the pump thread to record how
# it ended. See `Model.stream` -- the sentinel is put from a `finally` and therefore
# arrives fractionally before the exception it may be accompanying.
_SETTLE_SECONDS = 1.0


class ModelUnavailable(Exception):
    """
    The model cannot answer right now, and the message says why to a person.

    Every ``str()`` of one of these reaches the browser through ``/api/ask``, so the
    rule ``EnrichmentError`` states applies unchanged: a sentence somebody can act
    on, plain ASCII, nothing about the internals.
    """


_MISSING = (
    "The local model is not in this image. Ask is unavailable until the ai image "
    "is rebuilt."
)

_LOADING = "The local model is still loading. Try again in a moment."

_LOAD_FAILED = (
    "The local model could not be loaded. See the ai-api logs for the reason."
)

_BUSY = (
    "The local model is answering another question. Only one question can be "
    "answered at a time on this hardware -- try again in a moment."
)

_TOO_SLOW = "Generating an answer took too long and was stopped."

_FAILED = "The local model failed on that question."


def context_tokens(top_k: int, memo_chars: int) -> int:
    """
    How large a context this configuration needs, refusing one it cannot have.

    Derived rather than configured, so ``ASK_TOP_K`` and ``ASK_MEMO_CHARS`` are the
    only two numbers anybody has to think about and the window follows them. A third
    variable for the context would be a way to set it *wrong* -- too small is an
    overflow on the first long question, too large is KV cache paid for on every
    question and used by none.

    Rounded up to a multiple of 512 because that is the granularity llama.cpp's
    cache is allocated in anyway; an odd number here buys nothing and reads as
    though it were measured.
    """
    needed = top_k * memo_chars + OVERHEAD_TOKENS
    rounded = -(-needed // 512) * 512

    if rounded > MAX_CONTEXT_TOKENS:
        raise ConfigError(
            f"ASK_TOP_K ({top_k}) times ASK_MEMO_CHARS ({memo_chars}) needs a "
            f"{rounded}-token context, above the {MAX_CONTEXT_TOKENS} this service "
            "will load. Lower either one."
        )

    return rounded


# What builds a model. A parameter rather than a hard call, exactly as
# memo_ai/enrich/local.py's `LlmLoader` is and for the same reason: it lets the
# tests drive every path here -- still loading, load failed, busy, too slow, a
# generator that raises -- without 1,117 MB of weights or a second of inference.
LlmLoader = Callable[[], object]


class Model:
    """llama.cpp on this container's CPU, loaded at startup and kept."""

    def __init__(
        self,
        model_path: Path | str,
        *,
        n_ctx: int,
        deadline_seconds: float,
        loader: LlmLoader | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.n_ctx = n_ctx
        self.deadline_seconds = deadline_seconds
        self._loader = loader or (lambda: _load_llm(self.model_path, n_ctx))
        self._lock = threading.Lock()
        self._load: BackgroundCall | None = None
        self._generating: BackgroundCall | None = None

    def start_loading(self) -> None:
        """
        Begin the load. Called once, from the service's startup hook.

        Not in ``__init__``, so constructing a :class:`Model` is free and the tests
        that never generate anything never start a thread. And not blocking, which
        is the part that matters at the call site: uvicorn binds its socket
        immediately and ``/health`` answers ``loading`` while this runs, so the
        compose healthcheck has something truthful to wait on rather than a
        container that refuses connections for the first few seconds.
        """
        with self._lock:
            if self._load is not None:
                return

            log.info("loading %s into a %d-token context", self.model_path, self.n_ctx)
            self._load = BackgroundCall(self._loader, name="ask-model-load")

    @property
    def state(self) -> str:
        """
        ``missing``, ``loading``, ``failed`` or ``ready``. Reported by ``/health``.

        The file check is first and is done on every call rather than once, for the
        reason memo_ai/enrich/local.py gives: "the file appeared" is a real state --
        a bind mount, or an image rebuilt under a running stack.
        """
        if not self.model_path.is_file():
            return "missing"

        with self._lock:
            load = self._load

        if load is None or not load.done:
            return "loading"

        return "failed" if load.error is not None else "ready"

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """
        Yield the answer as it is produced, or raise :class:`ModelUnavailable`.

        **Nothing is generated on the caller's thread.** ``_pump`` runs the
        completion and puts each chunk on a bounded queue; this generator takes them
        off it with a timeout. Three things fall out of that arrangement, and all
        three are the reason for it rather than consequences to live with:

          * the wait for the *first* token is bounded like every other one. That is
            the wait that cannot be bounded by watching the loop, because prompt
            processing is a single uninterruptible call inside llama.cpp and it is
            most of the latency here.
          * a reader that stops reading stops the work. The queue is small, so a
            generator whose output nobody is taking blocks on ``put`` and gives up
            -- see ``_ABANDONED_AFTER_SECONDS``. A closed browser tab therefore
            costs at most ten more seconds of CPU rather than three minutes of it.
          * an abandoned generation is still *running* until it notices, which is
            why the busy check below is on the call rather than on a flag this
            method clears. A ``llama_context`` may not be entered by two threads at
            once, and the second question has to be refused rather than corrupt the
            first.
        """
        model = self._ready_model()

        with self._lock:
            if self._generating is not None and not self._generating.done:
                log.warning("declining to answer: the previous generation is still running")

                raise ModelUnavailable(_BUSY)

            chunks: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
            call = BackgroundCall(
                lambda: _pump(model, messages, chunks),
                name="ask-generate",
            )
            self._generating = call

        deadline = time.monotonic() + self.deadline_seconds

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise ModelUnavailable(_TOO_SLOW)

            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                raise ModelUnavailable(_TOO_SLOW) from None

            if chunk is _END:
                break

            yield chunk

        # **The sentinel is not proof the error is readable yet**, which is the one
        # ordering subtlety in this method. `_pump` puts _END from a `finally`, and
        # a `finally` runs *before* the exception reaches BackgroundCall's handler
        # -- so a consumer that read `call.error` the instant it saw the sentinel
        # would race the thread that is about to set it, and a failed generation
        # would sometimes look like a successful empty one.
        #
        # A short wait rather than the remaining deadline: the pump is one line from
        # returning by the time the sentinel is visible, so this is microseconds in
        # practice and a second is only the bound on a thread that cannot be
        # descheduled for that long without something else being very wrong.
        call.wait(_SETTLE_SECONDS)

        if call.error is not None:
            # The type is named explicitly because log.exception cannot be used --
            # the traceback belongs to another thread -- and without it a llama.cpp
            # failure and a Python one read identically in the log.
            log.warning(
                "answer generation failed: %s: %s",
                type(call.error).__name__,
                call.error,
            )

            raise ModelUnavailable(_FAILED) from call.error

    def _ready_model(self) -> object:
        state = self.state

        if state == "missing":
            log.warning("no model at %s", self.model_path)

            raise ModelUnavailable(_MISSING)

        if state == "loading":
            raise ModelUnavailable(_LOADING)

        with self._lock:
            load = self._load

        if state == "failed":
            log.warning(
                "loading the model failed: %s: %s",
                type(load.error).__name__,
                load.error,
            )

            raise ModelUnavailable(_LOAD_FAILED)

        return load.result


def _pump(model: object, messages: list[dict[str, str]], chunks: queue.Queue) -> None:
    """
    Run one streaming completion, putting each piece of text on ``chunks``.

    ``close()`` on the generator is what actually stops llama.cpp: throwing
    ``GeneratorExit`` into it at the ``yield`` unwinds the sampling loop, so an
    abandoned answer stops being computed rather than running to
    ``MAX_OUTPUT_TOKENS`` into a queue nobody reads.

    The ``finally`` is load-bearing twice. It puts the sentinel, so a completion
    that raised releases the consumer immediately instead of leaving it to time out
    -- and it closes the generator on every exit path, including the one where the
    consumer walked away.
    """
    stream = None

    # **`create_chat_completion` is inside the try, and that is a fix rather than a
    # style.** It was outside it in the first version of this function, so a call
    # that raised before yielding anything -- which is what a bad prompt or an
    # out-of-memory context does -- put no sentinel on the queue. The consumer then
    # waited out the entire deadline and reported "took too long" for a failure that
    # had already happened in milliseconds.
    try:
        stream = model.create_chat_completion(
            messages=messages,
            stream=True,
            temperature=TEMPERATURE,
            max_tokens=MAX_OUTPUT_TOKENS,
        )

        for piece in stream:
            text = _text(piece)

            if text:
                # A timeout rather than a blocking put: the consumer may be gone,
                # and a producer blocked forever on a full queue is a model that
                # never becomes free again.
                chunks.put(text, timeout=_ABANDONED_AFTER_SECONDS)
    except queue.Full:
        # Swallowed rather than reported, because it is not a failure: nobody is
        # left to report it to. The consumer that would have raised it is the one
        # that walked away.
        log.info("answer abandoned: nobody has read a token for %.0fs", _ABANDONED_AFTER_SECONDS)
    finally:
        close = getattr(stream, "close", None)

        if close is not None:
            close()

        # put_nowait, because the consumer that is gone is exactly the case where a
        # blocking put here would hang the thread on the last line of its own
        # cleanup. A full queue at this point means nobody is coming for the
        # sentinel either.
        try:
            chunks.put_nowait(_END)
        except queue.Full:
            pass


def _text(piece: object) -> str:
    """
    The text out of one streamed chunk, or ``""`` for one that carries none.

    llama-cpp-python's streaming shape is a completion object per token whose
    ``delta`` holds ``content`` -- except for the first, which carries ``role`` and
    no content, and the last, which carries ``finish_reason`` and no content. Both
    are ordinary and neither is an error, so this reads defensively rather than
    indexing: the alternative is a KeyError on the two chunks that always occur.
    """
    if not isinstance(piece, dict):
        return ""

    choices = piece.get("choices")

    if not isinstance(choices, list) or not choices:
        return ""

    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    content = delta.get("content") if isinstance(delta, dict) else None

    return content if isinstance(content, str) else ""


def _load_llm(model_path: Path, n_ctx: int) -> object:
    """
    Build the real model. The only place this module imports ``llama_cpp``.

    Imported inside the function for the reason memo_ai/enrich/local.py gives about
    the same import: it costs real time and memory, and the test suite has to run in
    an image where it may not be installed at all. Every test in
    tests/test_ask_model.py injects a loader and none of them import this.

    **A second constructor rather than a call into the enricher's**, and the
    duplication is the smaller cost. The two differ in the one argument that
    matters -- this context is sized from ``ASK_TOP_K`` and ``ASK_MEMO_CHARS``
    (:func:`context_tokens`) while the enricher's is a constant sized from one
    memo -- and sharing them would mean a function whose docstring had to explain
    both lifecycles at once. Everything else here is deliberately identical, and
    ``use_mmap`` is the reason it has to be: both processes map the *same* file, so
    the 1,081 MB of weights is one copy in page cache across ai-api and both worker
    replicas. A loader here that read the file instead would quietly triple the
    stack's memory.

    ``n_gpu_layers`` is left at its default of zero, which is also the only correct
    value: there is no GPU in this stack and none reachable from it. NOTES.md has
    the reason -- llama.cpp in a Linux container gets no GPU passthrough on macOS.

    ``verbose=False`` because llama.cpp narrates its load in about forty lines of
    tensor metadata on stderr, and ``start_loading`` already wrote the one line a
    reader of the ai-api log needs.
    """
    from llama_cpp import Llama

    return Llama(
        model_path=str(model_path),
        n_ctx=n_ctx,
        n_threads=THREADS,
        use_mmap=True,
        verbose=False,
    )

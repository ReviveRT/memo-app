"""
The real provider: faster-whisper, in this container, with no key and no network.

Three properties, and the third is what makes this more than a wrapper around a
library call.

**Constructing it loads nothing.** The model is fetched on the first voice memo
and kept for the life of the process. A worker that only ever sees text memos
never pays for it, and a bad ``STT_MODEL`` fails that one memo instead of turning
``restart: unless-stopped`` into a restart loop. That rule was set by
``UnimplementedStt`` when ``local`` was a placeholder, and it survives the
placeholder becoming real -- the committed default configuration is the one that
must not fail at boot.

**Every failure it can produce has a sentence.** Model load, download, OOM,
unreadable audio, silence, and inference that runs away are each classified into
:class:`SttUnavailable` (try the fallback) or :class:`SttError` (the fallback
will do no better), with a message written here rather than borrowed from a
library. ``last_error`` reaches the browser; ``memo_ai/audio.py`` applies the same
rule to ffmpeg's stderr and ``stt/base.py`` states it.

**It cannot pin a replica indefinitely.** Two bounds, and they are different
shapes because the two risks are:

  * *The download.* A cold cache pulls ~145 MB from HuggingFace. It runs on a
    daemon thread, and a memo that waits longer than
    :data:`MODEL_LOAD_TIMEOUT_SECONDS` gives up while the thread keeps going --
    so the job fails fast with a readable reason and the *next* memo finds the
    model warm, instead of one memo occupying a replica for the length of a bad
    connection. MEMO-15 bakes the weights into the image and makes this the rare
    path; until then it is the first voice memo after every clean build.
  * *The decode.* Bounded by a deadline scaled to the audio's own length. See
    :func:`_deadline_seconds`.

Measured on this machine (arm64, Docker Desktop, ``base``/int8, default beam size,
through the running stack): a five-second browser recording becomes a `ready` row
2.0 s after it is claimed, ffmpeg included. The worst 600-second input tried -- a
deliberately pathological one, the same five seconds looped for ten minutes, which
is the shape whisper repeats itself on -- took 125 s, or 0.21x realtime. So the
longest memo ``MAX_AUDIO_SECONDS`` admits is bounded at roughly two minutes rather
than the seconds a short one takes.

The first voice memo after a clean build pays for the model as well: 9.2 s
end-to-end including the download, against 2.0 s for the next one. A cached model
loads in under half a second.
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from memo_ai import audio
from memo_ai.stt.base import SttError, SttUnavailable, Transcript

log = logging.getLogger(__name__)

# int8 rather than the library's `default`, which resolves to float32 on CPU.
# There is no GPU in this stack and none assumed, so quantized weights are both
# faster and a third of the memory -- and memory is the shared resource here,
# since docker-compose.yml runs two replicas of this image.
COMPUTE_TYPE = "int8"

# `cpu_threads` is deliberately not passed. CTranslate2's default is 4 threads,
# not "every core" -- checked in faster_whisper.transcribe's own signature docs
# rather than assumed -- so two replicas come to 8, which is already the right
# shape for the `replicas: 2` in docker-compose.yml. Pinning a number here would
# hardcode that replica count into the Python, where it is not visible.

# How long one memo waits for the model before giving up on it.
#
# Generous, because the thing it is waiting for is a download and the penalty for
# being wrong is a failed memo. Not unbounded, because a stalled transfer would
# otherwise hold a replica for as long as the socket stays open.
MODEL_LOAD_TIMEOUT_SECONDS = 120.0

# The whole-job decode deadline: a multiple of the audio's own length, with a
# floor. See _deadline_seconds for both numbers.
DEADLINE_REALTIME_FACTOR = 4.0
DEADLINE_FLOOR_SECONDS = 120.0

_STILL_LOADING = (
    "The local transcription model is still being downloaded. Recordings will "
    "transcribe once it is ready — try this one again in a minute."
)

_LOAD_FAILED = (
    "The local transcription model {model!r} could not be loaded. Check STT_MODEL, "
    "and the ai-worker logs for the reason."
)

_OUT_OF_MEMORY = (
    "The transcription engine ran out of memory on this recording. A smaller "
    "STT_MODEL, or fewer ai-worker replicas, would leave it more room."
)

# Distinct from memo_ai/audio.py's _UNDECODABLE, and the difference is worth
# keeping: ffmpeg already accepted this audio and rewrote it, so a file the
# decoder here cannot open is a fault in the normalized copy rather than in
# whatever the user recorded. Nobody should be told to re-record over it.
_UNREADABLE = (
    "The prepared copy of this recording could not be read for transcription. "
    "Retrying the memo will make a fresh one."
)

_EMPTY = "This recording is empty — there is no audio in it to transcribe."

_NO_SPEECH = (
    "No speech was detected in this recording. It may be silent, or too quiet "
    "for the microphone that captured it."
)

_TOO_SLOW = (
    "Transcribing this recording took too long and was stopped. The server may "
    "be overloaded, or STT_MODEL may be too large for it."
)

# What builds a model. A parameter rather than a hard call so the tests can drive
# every path in this file -- load timeout, load failure, OOM, silence, a runaway
# decode -- without a 145 MB download or a second of inference. The real one is
# _load_whisper_model below.
ModelLoader = Callable[[str], object]


class LocalWhisperStt:
    """faster-whisper on the CPU of whichever machine is running the stack."""

    name = "local"

    def __init__(self, model_size: str, loader: ModelLoader | None = None) -> None:
        # WAV, opting out of the Opus default. This provider decodes in-process,
        # so a codec between ffmpeg and the model is two extra conversions for a
        # file that never leaves the container -- memo_ai/audio.py's `WAV` exists
        # for exactly this caller and says so.
        #
        # Opting out is a preference, not a requirement, and that matters for the
        # fallback chain: a `local` sitting behind a primary that wanted Opus is
        # handed Opus and transcribes it fine. Checked rather than assumed --
        # tests/test_local_whisper.py runs both formats through this provider and
        # asserts the transcripts match.
        #
        # An instance attribute rather than a class one, which is the shape
        # `audio.format_for` reads either way, and it is the import graph that
        # decides between them. memo_ai/audio.py imports SttError from
        # memo_ai/stt/base.py, which runs this package's __init__, which imports
        # this module -- so at the moment this class body executes, `audio` is
        # half-built and has no WAV yet. By the time an instance exists it is
        # whole. The alternative is a fourth module holding two dataclass
        # instances, which is a worse trade than one line in a constructor.
        self.audio_format = audio.WAV

        self.model_size = model_size
        self._loader = loader or _load_whisper_model
        self._lock = threading.Lock()
        self._model: object | None = None
        self._load: "_BackgroundLoad | None" = None

    def transcribe(self, source: Path) -> Transcript:
        model = self._ready_model()
        started = time.monotonic()

        try:
            # vad_filter is off by default in faster-whisper and is switched on
            # here for two reasons. Whisper is well known for inventing text over
            # silence -- subtitle credits, "Thank you." -- and this is what keeps
            # a memo of a quiet room from arriving as a plausible sentence nobody
            # said. It is also what makes _NO_SPEECH detectable at all: with the
            # speech segments gone, the transcript is empty rather than
            # hallucinated. Silero ships inside the faster-whisper wheel, so it
            # costs no download and works with networking disabled -- verified
            # under `docker run --network none`.
            #
            # Everything else is the library's default, including beam_size=5 and
            # automatic language detection. Nothing here is told what language to
            # expect and nothing needs to be: the Russian fixture is detected as
            # `ru` at 0.90 and transcribed without configuration.
            #
            # beam_size is left at the default rather than dropped to 1, and the
            # speed argument for dropping it is real: 35 s against 125 s on a
            # deliberately pathological ten-minute file. It is declined because
            # the number it buys is not one anybody is waiting on -- two minutes
            # for the longest memo this app accepts, seconds for a normal one --
            # and because greedy decoding is a quality trade that was not
            # measured here. The two settings did produce different text on that
            # file; which is *better* is not something a character count answers,
            # so the library's tuned default stands.
            segments, info = model.transcribe(str(source), vad_filter=True)

            if info.duration <= 0:
                raise SttError(_EMPTY)

            text = self._decode(segments, started, info.duration)
        except SttError:
            raise
        except MemoryError:
            log.warning("memo audio %s: whisper ran out of memory", source.name)

            raise SttUnavailable(_OUT_OF_MEMORY) from None
        except RuntimeError as error:
            # CTranslate2 reports engine-level failures -- allocation, an
            # unsupported compute type on this CPU, a corrupt model directory --
            # as RuntimeError from C++. Unavailable rather than a plain error:
            # none of it is a property of this recording, so another provider is
            # worth trying and so is this one on the next memo.
            log.warning("memo audio %s: whisper engine failure: %s", source.name, error)

            raise SttUnavailable(_OUT_OF_MEMORY if _is_allocation(error) else _UNREADABLE) from None
        except (OSError, ValueError) as error:
            # The decoder is PyAV, and it reports a missing file as a builtin
            # FileNotFoundError and undecodable bytes as a subclass of ValueError
            # -- both checked against av.error's MRO rather than assumed, so this
            # module needs no import of `av` to classify them. Neither should
            # happen: memo_ai/pipeline.py checked the original exists and ffmpeg
            # wrote this copy. If one does, the normalized temporary file is the
            # broken thing, not the upload.
            log.warning(
                "memo audio %s: whisper could not read the normalized copy: %s",
                source.name,
                error,
            )

            raise SttError(_UNREADABLE) from None

        if not text:
            raise SttError(_NO_SPEECH)

        return Transcript(text=text, provider=self.name, model=self.model_size)

    def _decode(self, segments, started: float, audio_seconds: float) -> str:
        """
        Drain the segment generator, giving up if it runs past the deadline.

        The generator is where the model actually runs: ``transcribe()`` returns
        after decoding the audio, running the VAD and identifying the language,
        and every segment after that is pulled lazily. Measured on a 600-second
        file, the call came back in 5.0 s and the first segment arrived at 10.1 s
        of a job that ran to 125 s at the shipped beam size. So iterating is the
        only place a deadline can be enforced without a thread to kill, and the
        cost of enforcing it there is that a stop happens up to one segment late.

        What that leaves unbounded is the eager half. It is linear in the length
        of the audio rather than in how hard the audio is, and the length is
        already capped by ``MAX_AUDIO_SECONDS``, so it is the wrong half to worry
        about -- the runaway case is a decode loop, and that is this one.
        """
        deadline = started + _deadline_seconds(audio_seconds)
        parts: list[str] = []

        for segment in segments:
            if time.monotonic() >= deadline:
                log.warning(
                    "whisper exceeded its %.0fs deadline on %.0fs of audio after %d segments",
                    deadline - started,
                    audio_seconds,
                    len(parts),
                )

                raise SttError(_TOO_SLOW)

            parts.append(segment.text)

        return "".join(parts).strip()

    def _ready_model(self):
        """
        The loaded model, loading it first if this is the first voice memo.

        Locked because the load runs on another thread and, once ``ai-api``
        (MEMO-24) exists, this image has an entrypoint that could call in from
        more than one. The lock is never held across the wait itself -- that
        would turn a slow download into a queue of blocked callers, each waiting
        the full timeout in turn instead of all of them watching the same load.
        """
        with self._lock:
            if self._model is not None:
                return self._model

            if self._load is None:
                log.info("loading whisper model %r (%s)", self.model_size, COMPUTE_TYPE)
                self._load = _BackgroundLoad(self._loader, self.model_size)

            pending = self._load

        if not pending.wait(MODEL_LOAD_TIMEOUT_SECONDS):
            # Left running on purpose. The thread is a daemon, so it cannot hold
            # up shutdown, and a download that needed longer than this memo could
            # wait is still worth finishing -- the next memo, or MEMO-16's retry
            # of this one, finds it done.
            raise SttUnavailable(_STILL_LOADING)

        with self._lock:
            if pending.error is not None:
                # Cleared, so the next memo starts a fresh attempt. A load failure
                # is as likely to be a transient fetch as a permanent
                # misconfiguration, and caching the failure would make the first
                # kind permanent.
                self._load = None

                log.warning(
                    "loading whisper model %r failed: %s: %s",
                    self.model_size,
                    type(pending.error).__name__,
                    pending.error,
                )

                raise SttUnavailable(_LOAD_FAILED.format(model=self.model_size))

            self._model = pending.model
            self._load = None

            return self._model


class _BackgroundLoad:
    """
    One model load on a daemon thread, with the outcome readable from outside.

    A bare ``threading.Thread`` rather than ``concurrent.futures``, and the reason
    is shutdown. ``ThreadPoolExecutor`` registers an ``atexit`` hook that joins
    its workers, so a load this class has already given up waiting for would
    block the interpreter from exiting -- which is precisely the hang that
    ``MODEL_LOAD_TIMEOUT_SECONDS`` exists to prevent, moved from one memo to
    ``docker compose down``. A daemon thread is abandoned at exit instead.
    """

    def __init__(self, loader: ModelLoader, model_size: str) -> None:
        self.model: object | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()

        thread = threading.Thread(
            target=self._run,
            args=(loader, model_size),
            name="whisper-model-load",
            daemon=True,
        )
        thread.start()

    def _run(self, loader: ModelLoader, model_size: str) -> None:
        try:
            self.model = loader(model_size)
        except BaseException as error:  # noqa: BLE001 -- reported to the waiter, not swallowed
            self.error = error
        finally:
            # In the finally, so a loader that raises still releases whoever is
            # waiting on it. Without this a failed load reads exactly like a slow
            # one, for the full timeout, on every memo.
            self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)


def _load_whisper_model(model_size: str):
    """
    Build the real model. The only place this package imports faster-whisper.

    Imported here rather than at module scope because the import itself is the
    expensive part -- it pulls ctranslate2, onnxruntime, numpy and PyAV, about a
    second of startup -- and a worker on ``STT_PROVIDER=fake``, or one serving
    only text memos, has no use for any of it. It also keeps the test suite
    runnable in an image that has no faster-whisper in it, which is what the
    stubbed loader in tests/test_local_stt.py relies on.

    ``device="cpu"`` explicitly, against the library's ``"auto"``. There is no GPU
    in this stack, and an auto that found one would silently change both the
    memory profile and the numbers measured in this file's docstring.

    No ``local_files_only``: the first run on a clean checkout has an empty
    ``whisper-cache`` volume and has to fetch the weights. MEMO-15 bakes them into
    the image, at which point this call finds them locally and never reaches the
    network -- which is what makes the "works with networking disabled" criterion
    hold on a machine that has run once.

    Both replicas race for that first fetch, which was watched rather than
    reasoned about: on a fresh volume they issued their HEAD requests within the
    same second and were both transcribing four seconds later. It is safe because
    huggingface_hub locks around the shared cache -- there is a ``.locks``
    directory beside the snapshot to prove it -- and the volume came out 142 MB,
    one copy.
    """
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device="cpu", compute_type=COMPUTE_TYPE)


def _deadline_seconds(audio_seconds: float) -> float:
    """
    How long the decode gets: four times the length of the audio, at least two
    minutes.

    Four, because the slowest configuration this project documents is
    ``STT_MODEL=medium`` on a CPU, which lands around realtime -- so the factor is
    headroom over the worst *supported* setup rather than over the measured one.
    Against the measurement it is enormous: ``base`` ran at 0.06x realtime here,
    and 0.21x on a deliberately pathological input, so this fires on genuine
    runaway decoding and on nothing else. Deliberately generous, the same way
    ``NORMALIZE_TIMEOUT_SECONDS`` is.

    The floor covers short memos, where the job is mostly fixed overhead and four
    times a five-second recording would be a deadline of twenty seconds.

    Scaled to the audio rather than fixed, because a fixed ceiling that is safe
    for a ten-minute memo is useless on a five-second one -- and it is the short
    memo taking minutes that means something is wrong.
    """
    return max(DEADLINE_FLOOR_SECONDS, audio_seconds * DEADLINE_REALTIME_FACTOR)


def _is_allocation(error: RuntimeError) -> bool:
    """
    Whether a CTranslate2 RuntimeError is really an out-of-memory.

    A substring match, which is exactly as weak as it looks -- C++ allocation
    failures reach Python as ``std::bad_alloc`` in the message text and there is
    no code to switch on. It only chooses between two sentences, both of which
    are true enough to act on, so being wrong costs the reader a nudge toward the
    wrong knob rather than a wrong outcome.
    """
    return "alloc" in str(error).lower()

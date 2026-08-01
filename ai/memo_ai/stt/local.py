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
#
# Five minutes rather than the two this started at, because the default model
# went from 142 MB to 1.6 GB. Most of that wait is normally already spent by the
# time a memo arrives -- `prefetch` starts the download at boot -- so this covers
# the case where somebody records within a minute of `docker compose up` on a
# slow link, and holding one replica once beats failing their first memo.
MODEL_LOAD_TIMEOUT_SECONDS = 300.0

# The whole-job decode deadline: a multiple of the audio's own length, with a
# floor. See _deadline_seconds for both numbers.
DEADLINE_REALTIME_FACTOR = 4.0
DEADLINE_FLOOR_SECONDS = 120.0

# How much audio to keep on each side of every region the voice-activity filter
# calls speech. faster-whisper's default is 400 ms and it is not enough: it eats
# words.
#
# Found on a real recording rather than reasoned about. "I would like to place an
# order", spoken in an Indian accent, transcribes correctly with the filter off
# and as "I would like to blaze an order" with it on at the default padding --
# the /p/ burst falls inside the trimmed margin, and without it the plosive reads
# as /b/. Every other setting was ruled out first: the same clip is wrong at both
# audio formats, both model sizes above `small`, and with the language pinned or
# detected. It is the padding.
#
# A full second, against the 800 ms that was the smallest value to fix it, and
# both were checked against the filter-off baseline on all five real recordings
# available -- two of the user's and the three browser fixtures. 800 and 1000
# match it everywhere; 400 breaks exactly one. So this is margin on a threshold
# that is already clear of the edge, not a value tuned until one case passed.
#
# What it costs is that the filter now trims almost nothing: with
# min_silence_duration_ms at 2000, a gap has to exceed two seconds to be cut at
# all, and a second of it survives on each side. That is the intended shape. The
# filter's job here is to answer "is there any speech in this at all" and to
# drop long dead air -- not to tighten around words.
VAD_SPEECH_PAD_MS = 1000

# Plain ASCII, like every other sentence that can reach `memos.last_error` --
# memo_ai/audio.py's four are, and the one non-ASCII character anywhere near this
# column is the ellipsis MemoQueue._truncate appends. One column, one UI, one
# character set worth reasoning about.
_STILL_LOADING = (
    "The local transcription model is still being downloaded. Recordings will "
    "transcribe once it is ready. Try this one again in a minute."
)

_LOAD_FAILED = (
    "The local transcription model {model!r} could not be loaded. Check STT_MODEL, "
    "and the ai-worker logs for the reason."
)

_OUT_OF_MEMORY = (
    "The transcription engine ran out of memory on this recording. A smaller "
    "STT_MODEL, or fewer ai-worker replicas, would leave it more room."
)

# For an engine fault that is not an allocation -- a compute type this CPU cannot
# run, a half-written model directory. Its own sentence rather than _UNREADABLE
# below, which was the first version of this and blamed the wrong thing: none of
# these are properties of the recording, and telling somebody their audio could
# not be read would send them to re-record over a server problem.
_ENGINE_FAILURE = (
    "The local transcription engine failed on this recording. The ai-worker logs "
    "say why; nothing is wrong with the recording itself."
)

# Distinct from memo_ai/audio.py's _UNDECODABLE, and the difference is worth
# keeping: ffmpeg already accepted this audio and rewrote it, so a file the
# decoder here cannot open is a fault in the normalized copy rather than in
# whatever the user recorded. Nobody should be told to re-record over it.
_UNREADABLE = (
    "The prepared copy of this recording could not be read for transcription. "
    "Retrying the memo will make a fresh one."
)

_EMPTY = "This recording is empty. There is no audio in it to transcribe."

# Three causes, not two, and the third was found rather than predicted. A Chrome
# recording truncated to its first 4 KB does not fail in memo_ai/audio.py the way a
# corrupt file is supposed to: ffmpeg salvages 600 ms of header and lead-in from it
# and reports success, so the file reaches here intact and silent. Told only that
# their microphone might be too quiet, whoever uploaded it would go and check the
# microphone.
_NO_SPEECH = (
    "No speech was detected in this recording. It may be silent, too quiet for "
    "the microphone that captured it, or cut short before anything was said."
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

    def __init__(
        self,
        model_size: str,
        language: str | None = None,
        loader: ModelLoader | None = None,
    ) -> None:
        # WAV, opting out of the Opus default. This provider decodes in-process,
        # so a codec between ffmpeg and the model is two extra conversions for a
        # file that never leaves the container -- memo_ai/audio.py's `WAV` exists
        # for exactly this caller and says so.
        #
        # Opting out is a preference, not a requirement, and that matters for the
        # fallback chain: a `local` sitting behind a primary that wanted Opus is
        # handed Opus and transcribes it fine. Checked rather than assumed --
        # tests/test_local_whisper.py runs both formats through this provider and
        # asserts the same words come back.
        #
        # The same *words*, and the qualifier was earned. On `base` the two
        # outputs were byte-identical and this comment said so; on the
        # `large-v3-turbo` now shipped they differ by a trailing full stop. The
        # codec does reach the output, just not the content of it.
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
        self.language = language
        self._loader = loader or _load_whisper_model
        self._lock = threading.Lock()
        self._model: object | None = None
        self._load: "_BackgroundLoad | None" = None

    def transcribe(self, source: Path) -> Transcript:
        model = self._ready_model()
        started = time.monotonic()

        try:
            # vad_filter is off by default in faster-whisper and is switched on
            # here, with more padding than it ships with -- see VAD_SPEECH_PAD_MS
            # for the word it eats at the default.
            #
            # On, because whisper invents text over silence and is *confident*
            # about it. With the filter off, four seconds of digital silence
            # transcribes as "Thank you.", low-level hiss as "Obrigado.", and
            # both come back with no_speech_prob 0.00 -- so the model's own
            # confidence cannot be used to catch them and this filter is the only
            # thing that can. It is also what makes _NO_SPEECH detectable at all:
            # with no speech region found there are no segments, and an empty
            # transcript is a fact rather than a guess.
            #
            # Silero ships inside the faster-whisper wheel, so it costs no
            # download and works with networking disabled -- verified under
            # `docker run --network none`.
            #
            # `language` is None unless STT_LANGUAGE says otherwise, and None is
            # what makes the Russian fixture come back as Russian without being
            # told. Naming it buys about 30 percent of the job by skipping a
            # detection pass, and buys certainty on short or accented audio,
            # where detection is genuinely unreliable -- three seconds of
            # accented English scored 0.39, and one committed English fixture is
            # detected as Russian at 0.89. memo_ai/config.py has both numbers and
            # the reason the default is still to detect.
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
            segments, info = model.transcribe(
                str(source),
                language=self.language,
                vad_filter=True,
                vad_parameters={"speech_pad_ms": VAD_SPEECH_PAD_MS},
            )

            if info.duration <= 0:
                raise SttError(_EMPTY)

            text = self._decode(segments, started, info.duration)
        except SttError:
            raise
        except MemoryError:
            # The model size is the lever, so it is the thing worth logging: an
            # OOM under `medium` on two replicas is a different conversation from
            # one under `tiny`.
            log.warning("whisper ran out of memory on %s under model %r", source, self.model_size)

            raise SttUnavailable(_OUT_OF_MEMORY) from None
        except RuntimeError as error:
            # CTranslate2 reports engine-level failures -- allocation, an
            # unsupported compute type on this CPU, a corrupt model directory --
            # as RuntimeError from C++. Unavailable rather than a plain error:
            # none of it is a property of this recording, so another provider is
            # worth trying and so is this one on the next memo.
            log.warning("whisper engine failure: %s: %s", type(error).__name__, error)

            if _is_allocation(error):
                raise SttUnavailable(_OUT_OF_MEMORY) from None

            raise SttUnavailable(_ENGINE_FAILURE) from None
        except (OSError, ValueError) as error:
            # The decoder is PyAV, and it reports a missing file as a builtin
            # FileNotFoundError and undecodable bytes as a subclass of ValueError
            # -- both checked against av.error's MRO rather than assumed, so this
            # module needs no import of `av` to classify them. Neither should
            # happen: memo_ai/pipeline.py checked the original exists and ffmpeg
            # wrote this copy. If one does, the normalized temporary file is the
            # broken thing, not the upload.
            #
            # The path is logged in full rather than by name: every one of these
            # files is called `normalized.wav`, so the name alone identifies
            # nothing, while the temporary directory in the path is per-job.
            log.warning("whisper could not read %s: %s", source, error)

            raise SttError(_UNREADABLE) from None

        if not text:
            raise SttError(_NO_SPEECH)

        return Transcript(text=text, provider=self.name, model=self.model_size)

    def prefetch(self) -> None:
        """
        Start the load now, without waiting for it or caring whether it works.

        Called once at boot by memo_ai/worker/__main__.py, and it exists because
        the default model got eleven times bigger. Lazily loading 142 MB on the
        first voice memo was a pause; lazily loading 1.6 GB is a failed memo on
        any connection that cannot finish it inside
        ``MODEL_LOAD_TIMEOUT_SECONDS``. Starting at boot spends the download
        against the minutes between ``docker compose up`` and somebody actually
        pressing Record, which is time that was going to be idle anyway.

        This does not weaken the rule that construction loads nothing, and the
        distinction is worth being exact about. Resolving a provider is still
        free, boot still cannot fail on a model, and a worker whose primary is
        `fake` never gets here -- the chain only offers up its primary, so
        ``STT_PROVIDER=fake`` stays the way to run the queue without a download.
        What changed is that a worker configured for real transcription now
        fetches its model when it starts rather than when it is first asked,
        which is what a reader of `STT_PROVIDER=local` would expect anyway.

        Nothing is raised and nothing is waited on. A failure here is recorded on
        the handle and surfaces on the first memo, with the retry ``failed``
        already provides -- a download that did not work is not a reason to stop
        serving text memos.
        """
        with self._lock:
            if self._model is None and (self._load is None or self._load.failed):
                log.info(
                    "prefetching whisper model %r (%s) in the background",
                    self.model_size,
                    COMPUTE_TYPE,
                )
                self._load = _BackgroundLoad(self._loader, self.model_size)

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

        **Only the block below decides whether a load is needed, and a waiter
        never retires one.** That is the whole reason this reads the way it does.
        The obvious version has each caller clear ``self._load`` when its load
        turns out to have failed, and it is wrong as soon as there are two
        callers: they wake one after the other, so the first clears the handle
        and raises, a third caller starts a *fresh* load, and the second waiter
        -- still holding the old handle -- wipes that new one on its way out. The
        memo after it would start a third load while the second was still
        running, and on a cold cache each of those is 142 MB. Asking
        ``_BackgroundLoad`` whether it failed, in one place, under the lock,
        means no caller has to reason about whether what it is holding is still
        current.
        """
        with self._lock:
            if self._model is not None:
                return self._model

            if self._load is None or self._load.failed:
                log.info("loading whisper model %r (%s)", self.model_size, COMPUTE_TYPE)
                self._load = _BackgroundLoad(self._loader, self.model_size)

            pending = self._load

        if not pending.wait(MODEL_LOAD_TIMEOUT_SECONDS):
            # Left running on purpose, and left as `self._load` so the next memo
            # waits on this one rather than starting a second. The thread is a
            # daemon, so it cannot hold up shutdown, and a download that needed
            # longer than this memo could wait is still worth finishing -- the
            # next memo, or MEMO-16's retry of this one, finds it done.
            raise SttUnavailable(_STILL_LOADING)

        if pending.error is not None:
            # Not cleared here. It is left in place carrying its failure, and the
            # block above starts a fresh attempt for whoever comes next -- because
            # a load failure is as likely to be a transient fetch as a permanent
            # misconfiguration, and caching it would make the first kind permanent.
            log.warning(
                "loading whisper model %r failed: %s: %s",
                self.model_size,
                type(pending.error).__name__,
                pending.error,
            )

            raise SttUnavailable(_LOAD_FAILED.format(model=self.model_size))

        with self._lock:
            self._model = pending.model

        return pending.model


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

    @property
    def failed(self) -> bool:
        """
        Finished, and finished badly. Both halves matter.

        A load still in flight is not failed, which is what stops a memo that
        timed out waiting from causing the next one to start a second download of
        the same weights.
        """
        return self._done.is_set() and self.error is not None


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

    Scaling on a number from the file is only safe because that number is already
    bounded before this is called. ``audio_seconds`` is whisper's own read of the
    file, and ``audio.normalize`` measured the same file with ffprobe and raised
    ``AudioTooLong`` above ``MAX_AUDIO_SECONDS`` -- two decoders from the same
    project, on a file one of them wrote, agreeing to within a frame. So there is
    no input for which this returns an absurd deadline, and no need for a second
    ceiling on top to catch one.
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

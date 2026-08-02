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

**It cannot pin a replica indefinitely.** Two bounds, both of the same shape,
because both guard C++ that cannot be interrupted: run it on a daemon thread and
stop *waiting* at the deadline. :class:`_BackgroundCall` is that shape.

  * *The download.* A cold cache pulls 1.6 GB from HuggingFace. A memo that waits
    longer than :data:`MODEL_LOAD_TIMEOUT_SECONDS` gives up while the thread keeps
    going, so the job fails with a readable reason and the *next* memo finds the
    model warm -- instead of one memo occupying a replica for the length of a bad
    connection. :meth:`LocalWhisperStt.prefetch` normally spends this at boot.
  * *The decode.* A deadline scaled to the audio's own length; see
    :func:`_deadline_seconds`, and :meth:`LocalWhisperStt._decode` for why it has
    to be a thread rather than a check inside the loop.

**Short memos are decoded in series and long ones in batches**, which is a
speed/accuracy trade taken only where it pays. :data:`BATCH_ABOVE_SECONDS` has the
measurements on both sides of it.

**The language is guessed by a model that costs nothing**, and handed to the one
that does. Detection is a whole extra encoder pass, so letting the big model do it
doubles a short memo; :data:`DETECT_MODEL` runs it on ``tiny`` instead.

Measured uncontended on this machine (arm64, Docker Desktop, ``large-v3-turbo``
at int8, the default four CTranslate2 threads):

  ==================== ========== ========== ===============
  audio                in series  batched    what runs
  ==================== ========== ========== ===============
  3.1 s (real memo)      8.0 s      8.4 s    series
  13.1 s (real memo)     8.7 s      9.1 s    series
  120 s                143.0 s     27.5 s    batched
  600 s (projected)    ~10 min     ~2 min    batched
  ==================== ========== ========== ===============

Those are the transcription alone, with the language already known. End to end
through the running stack -- ffmpeg, the cheap detector, the poll interval and the
database included -- a three-second memo is a `ready` row in 5.1 s and a
thirteen-second one in 5.2 s, against 9.5 s and 9.0 s before the detector was
split out.

Peak resident is 1.65 GB per replica in series and 2.4 GB when a long memo
batches; ``tiny`` adds 75 MB of weights and no measurable memory.
"""

import logging
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from memo_ai import audio, failures, prose
from memo_ai.stt.base import SttError, SttUnavailable, Transcript

log = logging.getLogger(__name__)

# int8 rather than the library's `default`, which resolves to float32 on CPU.
# There is no GPU in this stack and none assumed, so quantized weights are both
# faster and a third of the memory -- and memory is the shared resource here,
# since docker-compose.yml runs two replicas of this image.
COMPUTE_TYPE = "int8"

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

# How many VAD-cut chunks the encoder runs at once, once batching is used at all.
#
# Eight rather than sixteen, which is the counterintuitive half: the larger batch
# was *slower* at both thread counts tried (31.6 s against 27.5 s on a
# two-minute recording) and no cheaper in memory, so there is nothing to buy by
# raising it.
BATCH_SIZE = 8

# Below this much audio, decode in series instead. Batching is a speed/accuracy
# trade and this is the line where the trade starts being worth taking.
#
# The speed is real and large, but only on long audio: a two-minute recording
# takes 143 s in series and 27.5 s batched, 0.97x realtime against 0.23x. On
# anything short it buys nothing at all -- batching works by running several
# 30-second windows through the encoder at once, and a thirteen-second memo is one
# window. Measured on the five real recordings available: 8.7 s against 9.1 s,
# 8.0 s against 8.4 s. Within noise, sometimes slower.
#
# The accuracy cost is not noise, and it is not a maybe. Each chunk is decoded
# independently, so whisper loses the running context that keeps its formatting
# consistent, and the same clips come back measurably worse -- checked three times
# each, byte-identical within a mode and always different between them:
#
#   "...need this to be write down at 12 p.m."   ->  "...to be right down at..."
#   "1, 2, 3, 4, 5, 6, 7, 8, 9, 10"              ->  "one two three four five..."
#
# Numerals and punctuation are worth keeping in a column that gets full-text
# searched, and "write" is the word that was said.
#
# So: series for the memos people actually record, where it is both more accurate
# and no slower, and batching only past the point where series decoding starts
# costing minutes. Two minutes of audio is that point -- 143 s of waiting -- and
# past it the arithmetic inverts, because a ten-minute memo is ten minutes in
# series against two batched, and nobody trades eight minutes for a comma.
BATCH_ABOVE_SECONDS = 120.0

# The two settings that stop whisper inventing words it never heard.
#
# The failure they fix is the classic one and it is spectacular: ten seconds of
# somebody saying "Rock" ten times came back as **223** of them, from 4.3 seconds
# of audio. The decoder gets stuck re-emitting a token and keeps going until it
# hits its own token ceiling -- which is also why that memo took 21 s instead of
# 5, since every one of those words had to be generated.
#
# `repetition_penalty` is the direct fix, and on its own it is *worse than
# useless*. Measured six times per setting on that recording:
#
#   baseline                       223 223 223 223 223 223
#   repetition_penalty=1.1           0   0   1   6   9 223
#   temperature=0                  223 223 223 223 223 223
#   temperature=0 + penalty=1.1     11  11  11  11  11  11
#
# The middle row is the point. A penalty alone swings between losing the memo
# entirely and not helping at all, because when whisper decides its own output
# looks degenerate it retries at a higher temperature -- and temperature above
# zero means *sampling*. The penalty changes which lottery ticket gets drawn
# rather than fixing anything.
#
# So the fallback ladder goes too. `temperature=0.0` is one rung, no retries, no
# sampling, and the same audio now produces the same transcript every time. What
# that gives up is whisper's own recovery path for a bad decode -- and on the one
# recording where it would have mattered it recovered to 223 rocks twenty times
# out of twenty, so it was buying nothing but variance and 11 seconds.
#
# Checked against every other real recording: "I would like to place an order"
# and all three browser fixtures come back byte-identical, silence still comes
# back empty, and the one difference anywhere is "I will" contracting to "I'll".
REPETITION_PENALTY = 1.1
TEMPERATURE = 0.0

# The sentence handed to the decoder before the audio, to make it punctuate.
#
# Whisper was trained on both punctuated and unpunctuated transcripts, and which
# style it produces is a property of the audio rather than a setting. When it
# chooses wrong it is spectacular: a real 89-second memo recorded into this app came
# back as 1204 characters of lowercase words with not one comma in them, while every
# short memo from the same session was punctuated correctly. Reproduced here
# whenever wanted, from that recording.
#
# `initial_prompt` is the fix, and it is a style prompt rather than an instruction:
# whisper receives it as if it were the transcript of the audio immediately before
# this, so a primer that is punctuated, capitalized and full of ordinary sentences
# is a demonstration of the register to continue in. On that recording it is the
# whole difference between the blob above and fifteen clean sentences.
#
# Measured on it, once per setting, with the *first* wording of the primer -- which
# is not the one below, for a reason two paragraphs down, but is what these four
# rows were run with:
#
#   baseline                                lowercase blob, 13 segments
#   condition_on_previous_text=False        lowercase blob, 13 segments
#   initial_prompt                          punctuated, 7 segments
#   initial_prompt + no conditioning        punctuated for four sentences, then blob
#
# The last row is the one that explains the mechanism, and it is why
# `condition_on_previous_text` is deliberately left at its default of True. The
# primer only reaches the first 30-second window directly; what carries its register
# through the rest of a long recording is whisper conditioning each window on the
# text of the one before. Switch that off and the style decays a few sentences in,
# which is exactly what the fourth row shows.
#
# It costs nothing measurable: 28.2 s against 27.7 s on that recording. The wording
# actually shipped punctuates the same recording just as fully, in 10 segments.
#
# **The wording is load-bearing, and both halves of it were paid for.**
#
#   * *The digits.* The first version of this primer was prose with no numeral in
#     it, and it taught the model to spell numbers out -- the browser fixture that
#     transcribes as "1, 2, 3, 4, 5, 6, 7, 8, 9, 10" came back as "One, two,
#     three...". That is a real loss in a column that gets full-text searched, and
#     BATCH_ABOVE_SECONDS already declined a faster decode path over the same
#     regression. "2 or 3" in the primer is what keeps numerals as numerals.
#   * *Everything else about it.* A third variant, also with digits but differently
#     phrased, reintroduced the repetition loop REPETITION_PENALTY exists to stop:
#     the same 89 seconds came back with "Let me clean up." repeated 25 times and 34
#     segments instead of 10. So this is not a slot for any punctuated sentence, and
#     editing it means re-running the recordings in tests/fixtures/ plus a long one.
#     The prompt perturbs decoding, and decoding here has a known way to fail.
#
# What the shipped primer does cost, stated because it is a real measurement and not
# a nil one: on the four deliberately degenerate recordings here -- somebody saying
# one word over and over, which is what REPETITION_PENALTY was found with -- it moves
# the count in both directions. Twice each, baseline against primed:
#
#   "Rock" x11, 4.3 s (two uploads)      11 -> 12
#   "Rock" x11, 6.2 s                    11 ->  5
#   "Rocka" x15, 6.8 s                   15 -> 17
#
# Every one of those is deterministic, and none is a runaway; the 5 is the primer
# reading a wall of identical words as prose and stopping early. That is the safer
# direction of the two, and none of it is a shape human speech takes -- these clips
# exist to provoke the decoder. It is recorded here so that nobody re-measuring the
# repetition table wonders why the numbers moved.
#
# English, on recordings that may not be. Checked rather than assumed: the Russian
# fixture still comes back Russian, detected at 0.99, with its words unchanged --
# because the language token decides the language and the prompt only suggests a
# register. What is *not* claimed is that an English primer punctuates a long
# non-English recording as well as it does this one; there is no long non-English
# recording here to find out on.
PUNCTUATION_PRIMER = (
    "Okay, here is a note to myself. It runs to 2 or 3 sentences, with commas "
    "where they belong and a full stop at the end. Does it read well? Yes, it does."
)

# The model that decides what language a recording is in, when STT_LANGUAGE has
# not already said.
#
# Detection is not a cheap add-on to transcription -- it is a whole extra encoder
# pass over the first 30-second window, and on a model whose encoder is the entire
# cost it simply doubles the job. Measured on the shipped model: 8.39 s for a
# three-second memo detecting, 4.09 s with the language given. Nothing else in
# this file is worth a factor of two.
#
# So the pass is run on `tiny` instead, and the answer handed to the real model.
# `tiny` is 75 MB and answers in 0.21 s against turbo's 4.44 s -- 21 times cheaper
# -- and on every real recording available it reached the *same verdict as turbo*,
# including the Russian one. `small` did not, which is the useful counter-example:
# it called an English recording Finnish at 0.74 confidence, so this is not simply
# "bigger detector, better answer" and the pick is empirical rather than obvious.
#
# The honest limits: five recordings, two languages. This makes a wrong language
# somewhat likelier than letting turbo decide, and a wrong language ruins a
# transcript rather than degrading it. DETECT_MIN_CONFIDENCE is the valve.
DETECT_MODEL = "tiny"

# Below this, discard `tiny`'s answer and let the real model detect for itself --
# paying the old double cost on that memo and only that memo.
#
# Low, because the evidence says confidence is a weak proxy for correctness rather
# than a good one: `tiny` was right at 0.69 on one recording, and `small` was wrong
# at 0.74 on another. A threshold tight enough to have caught `small`'s mistake
# would also have thrown away two correct answers. So this catches the flagrantly
# unsure case and makes no claim beyond that.
DETECT_MIN_CONFIDENCE = 0.5

# `cpu_threads` is still deliberately not passed, and this is now measured rather
# than argued. CTranslate2 defaults to 4. Raising it to 8 bought 10 percent on a
# 120-second memo (24.9 s against 27.5 s), made *short* memos slower (4.2 s
# against 3.9 s), and cost 890 MB of peak resident -- which on two replicas is 1.8
# GB for a tenth of the long-memo case. Batching already extracts the parallelism
# that raising this was meant to.

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
ModelLoader = Callable[[str, "str | None"], "_Engines"]


@dataclass(frozen=True)
class _Engines:
    """
    What one load produces: the model that transcribes, and the one that guesses
    the language for it.

    Both come out of a single :class:`_BackgroundCall`, rather than two, because
    the second is 75 MB beside the first's 1.6 GB -- not worth a second slot of
    load state, a second timeout, or a second thing that can be half-ready when a
    memo arrives.
    """

    transcriber: object

    # None when STT_LANGUAGE is set, because then there is nothing to detect and
    # no reason to hold a second model resident. Also None on a provider built
    # before a language was known, which cannot happen -- the language is a
    # constructor argument.
    detector: "_LanguageDetector | None"


class _LanguageDetector:
    """
    ``tiny``, used for one question and never asked to transcribe.

    A class rather than a bare model because ``detect_language`` wants decoded
    samples rather than a path -- it hands its argument straight to the feature
    extractor -- and the decoder that produces them is a faster-whisper import.
    Keeping both behind this object is what lets everything above it stay
    unaware that faster-whisper exists, and lets the tests substitute five lines.
    """

    def __init__(self, model: object, decode: Callable[[str], object]) -> None:
        self._model = model
        self._decode = decode

    def detect(self, source: Path) -> tuple[str, float]:
        language, probability, _ = self._model.detect_language(self._decode(str(source)))

        return language, probability


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
        self._load: "_BackgroundCall | None" = None

    def transcribe(self, source: Path) -> Transcript:
        engines = self._ready_model()

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
            options = {
                "language": self.language or _detected(engines.detector, source),
                "vad_filter": True,
                "vad_parameters": {"speech_pad_ms": VAD_SPEECH_PAD_MS},
                # See REPETITION_PENALTY. Applied to both decode paths, though
                # batching bounds a loop on its own -- independent windows cannot
                # carry one across them. Measured on the same recording, batching
                # alone gave 9 against the penalty's 11, and both with it gave 7;
                # one token either side of the truth on a pathological clip is
                # not worth two decode paths that behave differently.
                "temperature": TEMPERATURE,
                "repetition_penalty": REPETITION_PENALTY,
                # See PUNCTUATION_PRIMER, which also has the reason
                # `condition_on_previous_text` is left at its default beside it.
                "initial_prompt": PUNCTUATION_PRIMER,
            }
            seconds = _wav_seconds(source)
            transcriber = engines.transcriber

            if seconds is not None and seconds >= BATCH_ABOVE_SECONDS:
                # `transcriber` is the batched pipeline; `.model` is the plain one
                # underneath it. See BATCH_ABOVE_SECONDS for why only long audio
                # takes this path.
                segments, info = transcriber.transcribe(
                    str(source), batch_size=BATCH_SIZE, **options
                )
            else:
                segments, info = transcriber.model.transcribe(str(source), **options)

            if info.duration <= 0:
                # NO_AUDIO rather than the class default: nothing failed here, the
                # file simply has no audio in it. That decides whether the app keeps
                # the memo for a retry or throws it away -- memo_ai/failures.py.
                raise SttError(_EMPTY, code=failures.NO_AUDIO)

            # Shaped outside the deadline, and after `info` exists, because both
            # things it needs are here: the language the model actually decoded in,
            # which gates one of memo_ai/prose.py's rules, and the finished text.
            # The shaping itself is regular expressions over a few kilobytes and
            # does not want a timeout of its own.
            text = prose.shape(self._decode(segments, info.duration), info.language)
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

            raise SttError(_UNREADABLE, code=failures.UNREADABLE) from None

        if not text:
            # The one failure the person who made the recording caused and cannot
            # fix by retrying: there are no words in the file. Coded so the app can
            # discard the memo instead of leaving a card that says "you said
            # nothing" -- see memo_ai/failures.py, and web/src/memoFailure.js for
            # what is done with it.
            raise SttError(_NO_SPEECH, code=failures.NO_SPEECH)

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
                self._load = _BackgroundCall(
                    lambda: self._loader(self.model_size, self.language), name="whisper-model-load"
                )

    def _decode(self, segments, audio_seconds: float) -> str:
        """
        Drain the segment generator on a thread, and give up on it at the deadline.

        Draining is where the model actually runs. ``transcribe()`` returns almost
        at once -- 0.39 s on a 120-second recording -- having decoded the audio,
        cut it at silence and identified the language; pulling the first segment
        is what does the remaining 27 s of work.

        That timing is why this is a thread rather than a check inside the loop. A
        per-segment check was the first version, and against the *batched*
        pipeline it stops preempting anything: every segment materialises together
        on the first ``next()``, so by the time the loop could look at the clock
        the work is already paid for. A deadline that can only report is not a
        deadline.

        The thread is abandoned rather than stopped, because CTranslate2 runs in
        C++ and there is nothing to cancel. That is only worth doing if the next
        memo can still get on with its life, which was checked rather than
        assumed: two transcriptions on one model from two threads ran genuinely
        concurrently -- 27.5 s of overlap against 15.0 s solo -- so they share the
        cores rather than queueing. An abandoned runaway makes the replica slower
        until it finishes; it does not make it stuck, and it does finish, because
        batching decodes independent windows and cannot carry a repetition loop
        across them the way long-form sequential decoding can.
        """
        drain = _BackgroundCall(lambda: "".join(s.text for s in segments).strip())
        deadline = _deadline_seconds(audio_seconds)

        if not drain.wait(deadline):
            log.warning(
                "whisper exceeded its %.0fs deadline on %.0fs of audio",
                deadline,
                audio_seconds,
            )

            raise SttError(_TOO_SLOW, code=failures.TOO_SLOW)

        if drain.error is not None:
            # Raised in this thread so the caller's except clauses can classify it
            # -- a PyAV failure that happened over there is the same failure.
            raise drain.error

        return drain.result

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
        ``_BackgroundCall`` whether it failed, in one place, under the lock,
        means no caller has to reason about whether what it is holding is still
        current.
        """
        with self._lock:
            if self._model is not None:
                return self._model

            if self._load is None or self._load.failed:
                log.info("loading whisper model %r (%s)", self.model_size, COMPUTE_TYPE)
                self._load = _BackgroundCall(
                    lambda: self._loader(self.model_size, self.language), name="whisper-model-load"
                )

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
            self._model = pending.result

        return pending.result


class _BackgroundCall:
    """
    One callable on a daemon thread, with the outcome readable from outside.

    Two things in this file need the same shape -- loading the model, and draining
    the segment generator -- and both need it for the same reason: the work is C++
    that cannot be interrupted, so the only way to bound it is to stop *waiting*
    for it and let it finish unattended.

    A bare ``threading.Thread`` rather than ``concurrent.futures``, and the reason
    is shutdown. ``ThreadPoolExecutor`` registers an ``atexit`` hook that joins its
    workers, so a call this class has already given up waiting for would block the
    interpreter from exiting -- which is precisely the hang the timeouts exist to
    prevent, moved from one memo to ``docker compose down``. A daemon thread is
    abandoned at exit instead.
    """

    def __init__(self, work: Callable[[], object], name: str = "whisper") -> None:
        self.result: object | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()

        threading.Thread(target=self._run, args=(work,), name=name, daemon=True).start()

    def _run(self, work: Callable[[], object]) -> None:
        try:
            self.result = work()
        except BaseException as error:  # noqa: BLE001 -- reported to the waiter, not swallowed
            self.error = error
        finally:
            # In the finally, so work that raises still releases whoever is waiting
            # on it. Without this a failed call reads exactly like a slow one, for
            # the full timeout, every time.
            self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    @property
    def failed(self) -> bool:
        """
        Finished, and finished badly. Both halves matter.

        A call still in flight is not failed, which is what stops a memo that timed
        out waiting on a model download from causing the next one to start a second
        download of the same weights.
        """
        return self._done.is_set() and self.error is not None


def _load_whisper_model(model_size: str, language: str | None) -> "_Engines":
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
    directory beside the snapshot to prove it -- and the volume came out one copy.

    A :class:`BatchedInferencePipeline` rather than the bare model, which is what
    makes :data:`BATCH_SIZE` mean anything. It is a wrapper with the same
    ``transcribe`` shape -- same arguments, same ``(segments, info)`` back, same
    exceptions out of PyAV, all checked -- and ``.model`` is the plain one
    underneath, for the series path.

    The ``tiny`` detector is built alongside it unless ``language`` already says
    what to expect, in which case there is nothing to detect and no reason to keep
    a second model resident. :data:`DETECT_MODEL` has what it is for and what it
    saves.
    """
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    from faster_whisper.audio import decode_audio

    def build(size: str) -> object:
        return WhisperModel(size, device="cpu", compute_type=COMPUTE_TYPE)

    detector = None

    if language is None:
        detector = _LanguageDetector(build(DETECT_MODEL), decode_audio)

    return _Engines(
        transcriber=BatchedInferencePipeline(model=build(model_size)),
        detector=detector,
    )


def _detected(detector: "_LanguageDetector | None", source: Path) -> str | None:
    """
    What language to tell the real model, or ``None`` to let it work that out.

    ``None`` on three paths, and all three end in the same place -- the model
    detects for itself, at the cost this function exists to avoid:

      * no detector, which means ``STT_LANGUAGE`` is set and there is nothing to
        decide (that path never reaches here);
      * the guess came back under :data:`DETECT_MIN_CONFIDENCE`;
      * the detector raised. A failure to guess a language is not a reason to
        fail a memo, so it is logged and the slower path is taken.
    """
    if detector is None:
        return None

    try:
        language, probability = detector.detect(source)
    except Exception:
        # Broad on purpose. Everything this can raise -- a decode failure, a model
        # fault -- is about to be raised again by the transcription itself, on the
        # same file, where it is classified properly. Failing here would report it
        # as a language problem.
        log.warning("language detection failed; letting the model detect", exc_info=True)

        return None

    if probability < DETECT_MIN_CONFIDENCE:
        log.info(
            "language detection unsure (%s at %.2f), falling back to the full model",
            language,
            probability,
        )

        return None

    log.debug("detected language %s at %.2f", language, probability)

    return language


def _wav_seconds(source: Path) -> float | None:
    """
    How long a WAV is, from its header, or ``None`` for anything else.

    Needed before the model is called, which is the whole difficulty:
    :data:`BATCH_ABOVE_SECONDS` has to choose a decode path, and the duration
    whisper reports only arrives afterwards. ``memos.duration_ms`` has the same
    number and the pipeline already holds it, but it reaches a provider through
    ``transcribe(audio)`` and widening that signature would touch every provider
    and every test double to serve one branch in this file.

    ``wave`` from the standard library, so it costs a header read and no
    dependency. It works because this provider *asks* for WAV -- see
    ``audio_format`` -- and the answer is exact for PCM rather than estimated.

    ``None`` for anything that is not a WAV, which is the fallback chain handing
    this provider the Opus a different primary asked for. Unknown length means
    the series path, and that is the right way round: it is the more accurate one,
    and it is only slow on long audio, which is exactly the case this could not
    identify.
    """
    try:
        with wave.open(str(source), "rb") as handle:
            rate = handle.getframerate()

            return handle.getnframes() / rate if rate else None
    except (OSError, wave.Error):
        return None


def _deadline_seconds(audio_seconds: float) -> float:
    """
    How long the decode gets: four times the length of the audio, at least two
    minutes.

    Four, because the slowest configuration this project documents is
    ``STT_MODEL=medium`` on a CPU, which lands around realtime -- so the factor is
    headroom over the worst *supported* setup rather than over the measured one.
    Against the measurement it is enormous: the shipped model batched runs at
    0.23x realtime, so this fires on genuine runaway decoding and on nothing else.
    Deliberately generous, the same way ``NORMALIZE_TIMEOUT_SECONDS`` is.

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

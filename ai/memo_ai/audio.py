"""
One canonical format before STT, and a duration you can trust.

Two jobs, and the second is the reason the first cannot be skipped.

**One decode path.** Three browsers produce three containers -- Chrome WebM/Opus,
Firefox Ogg/Opus, Safari MP4/AAC -- and every consumer downstream would otherwise
need to handle all three. ffmpeg handles them here, once, and everything after
this module sees 16 kHz mono.

**A duration you can trust.** MediaRecorder writes its container to a
non-seekable sink, so it never goes back to fill in the duration: a Chrome
recording arrives with no Duration element in the Segment Info and no Cues.
``ffprobe`` on that file answers ``N/A``, not a number. Confirmed here rather
than taken on trust -- the same tone encoded to a *seekable* WebM reports
7.308000 and the identical encode written to a pipe reports ``N/A``. So the
duration has to come from the *normalized* file, which ffmpeg wrote to a real
path and could finish properly. That is what makes ``memos.duration_ms``
trustworthy and what makes ``MAX_AUDIO_SECONDS`` enforceable at all.

What this does **not** do is save money, and an earlier version of this comment
said it did. Hosted transcription is billed per minute of audio *duration*, so
resampling changes the bill by exactly zero. The reasons are the two above, plus
one for MEMO-14: 16 kHz mono is whisper's native input, so the local path does
not resample again.
"""

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from memo_ai.stt.base import SttError, SttProvider

log = logging.getLogger(__name__)

# Whisper's native input, and the only reason to pick a number at all. Anything
# above this is discarded by every provider the project intends to use, so
# carrying it costs bytes and buys nothing.
SAMPLE_RATE = 16_000
CHANNELS = 1

# Wall-clock ceilings, so a malformed file cannot wedge a worker replica forever.
#
# Generous rather than tuned. Encoding is far faster than realtime -- a ten-minute
# memo is a few seconds of ffmpeg -- and the input is already bounded by
# MAX_AUDIO_BYTES (12 MiB) at the API edge, so the pathological case is not "a
# hundred hours of audio" but "twelve megabytes that decode to an implausible
# number of hours". 120 seconds covers that and still fails rather than hangs.
#
# This is the guard that lets normalization run *before* the duration is known.
# Probing the source first to skip the encode was the obvious alternative and it
# does not work: the source is exactly the file whose duration is N/A.
NORMALIZE_TIMEOUT_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 30.0

# One sentence for two different ways of not getting a number, because the
# difference between them is not one the reader of a memo can act on.
_UNMEASURABLE = "The length of this recording could not be determined, so it was not transcribed."

# For bytes ffmpeg cannot read at all, as opposed to a file it reads fine and finds
# nothing to transcribe in. Those are different sentences on purpose -- see
# _require_audio_stream.
_UNDECODABLE = (
    "This recording could not be decoded. It may be incomplete or in a format "
    "this server does not support."
)


@dataclass(frozen=True)
class AudioFormat:
    """
    What to normalize *to*. Two exist, and the choice is a provider's.

    Both are 16 kHz mono; they differ in what they cost to carry. See
    :data:`OPUS` and :data:`WAV`.
    """

    name: str
    suffix: str

    # Codec flags only. The rate and channel flags are the same for both and are
    # added by _ffmpeg_command, so a new format cannot forget them.
    codec_args: tuple[str, ...]


# The default, and the one a hosted provider must use.
#
# The number that decides it is WAV's, and it is the one that can be checked
# exactly, because PCM does not depend on content: 600 seconds -- a memo at the
# full MAX_AUDIO_SECONDS -- is **19.20 MB** at 16 kHz mono 16-bit, which is **77
# percent** of OpenAI's 25 MB request limit. Both measured here. So WAV fits, with
# almost nothing to spare, on the longest memo this app accepts.
#
# Against that, this app uploads that same memo in about 3.7 MB: MEMO-11 asks
# MediaRecorder for 48 kbps and measured 49 kbps on the wire through the real
# Record button. Normalizing to WAV would therefore carry roughly five times the
# bytes the user actually recorded, to say the same words.
#
# The ticket puts the Opus output at 2.6 MB and that is *not* confirmed here, on
# purpose: Opus is variable-bitrate, so its size is a property of the content, and
# the synthetic tone available to this task encodes to 3.05 MB rather than 2.6 MB.
# Either figure is a fraction of WAV's, which is all the decision needs. Real
# recordings would settle it -- see ai/tests/fixtures/.
#
# An earlier version of this comment compared WAV against a 12.3 MB input, which is
# what a Chrome recording costs at its *default* bitrate. This app stopped
# producing those at MEMO-11.
#
# The cost of choosing Opus is one decode step on the local path, which is why WAV
# exists below rather than being deleted.
OPUS = AudioFormat("opus", ".opus", ("-c:a", "libopus", "-b:a", "24k"))

# For a provider that decodes in-process and wants no codec in the way -- which is
# MEMO-14's faster-whisper, and nothing today. Registered now because the *choice*
# is what this module is exporting, and a seam with one option is not a seam.
#
# Not the default, for the size reason above: a provider gets WAV by asking.
WAV = AudioFormat("wav", ".wav", ("-c:a", "pcm_s16le"))

DEFAULT_FORMAT = OPUS


class AudioError(SttError):
    """
    Audio that could not be prepared for transcription.

    An ``SttError`` rather than a hierarchy of its own, because from the memo's
    side the outcome is identical: no transcript, and one sentence written to
    ``memos.last_error`` for a person to read. memo_ai/pipeline.py already raises
    ``SttError`` for two non-STT reasons (a memo owing a transcript with no
    audio, and a key that escapes ``AUDIO_DIR``), so this follows the rule that
    file rather than inventing a second one.

    Deliberately **not** ``SttUnavailable``. That subclass means "try the
    fallback provider", and MEMO-14's fallback chain would walk it for nothing:
    every provider in the chain is fed by this module, so a file ffmpeg cannot
    decode is a file none of them will transcribe.
    """


class AudioTooLong(AudioError):
    """
    Longer than ``MAX_AUDIO_SECONDS``, refused before any provider is called.

    Carries the duration because the row wants it: a memo refused for length
    still records *how* long it was, which is the number its ``last_error`` is
    talking about. Without this attribute the failure write would have nothing to
    put in ``duration_ms`` and the UI would show a memo rejected for its length
    beside a blank length.
    """

    def __init__(self, message: str, duration_ms: int) -> None:
        super().__init__(message)
        self.duration_ms = duration_ms


@dataclass(frozen=True)
class NormalizedAudio:
    """The file a provider is given, and the duration measured off it."""

    path: Path
    duration_ms: int
    format: AudioFormat


def format_for(provider: SttProvider) -> AudioFormat:
    """
    The format this provider wants, defaulting to Opus.

    ``getattr`` rather than a member of the ``SttProvider`` protocol, and both
    halves of that are deliberate. A required member would mean the "five-line
    class that imports nothing" test double in that protocol's docstring stops
    being five lines and starts importing this module. And ``stt/base.py``
    cannot name :class:`AudioFormat` anyway without an import cycle, since this
    module imports ``SttError`` from it.

    So the attribute is optional and the default is the safe one: a provider that
    says nothing gets Opus, which is small enough for a hosted request and
    decodable by everything. Asking for WAV is opting *out* of that.
    """
    fmt = getattr(provider, "audio_format", DEFAULT_FORMAT)

    return fmt if isinstance(fmt, AudioFormat) else DEFAULT_FORMAT


@contextmanager
def normalize(source: Path, fmt: AudioFormat, max_seconds: float) -> Iterator[NormalizedAudio]:
    """
    Transcode ``source`` to 16 kHz mono, measure it, and enforce the cap.

    A context manager because the output is a temporary derivative and nothing
    should be left holding it. The original on the ``audio`` volume is untouched
    -- MEMO-23 serves playback from that file, so normalization has to be
    non-destructive, and the normalized copy has no reason to outlive the job.

    ``/tmp`` on the container's writable layer, not ``AUDIO_DIR``. Writing
    derivatives into the shared volume would put files beside the originals that
    look like memo audio and are not, on the one volume two containers share.

    Raises :class:`AudioTooLong` when the measured duration is over
    ``max_seconds``, before the caller ever reaches the provider -- which is the
    whole point of measuring here rather than after transcription.
    """
    _require_audio_stream(source)

    # ignore_cleanup_errors, because of *when* this cleanup runs. It happens on the
    # way out of the caller's `with`, which is after the provider has already
    # returned a transcript -- so an OSError raised here would propagate into
    # memo_ai/pipeline.py's generic handler and fail a memo that had just been
    # transcribed successfully, discarding work that on a hosted provider was paid
    # for. MEMO-16's rule is that a transcript is never lost, and a leaked file in
    # /tmp is a much smaller problem than losing one.
    with tempfile.TemporaryDirectory(
        prefix="memo-normalize-", ignore_cleanup_errors=True
    ) as scratch:
        destination = Path(scratch) / f"normalized{fmt.suffix}"

        _run(
            _ffmpeg_command(source, destination, fmt),
            NORMALIZE_TIMEOUT_SECONDS,
            _UNDECODABLE,
        )

        duration_ms = _probe_duration_ms(destination)

        if duration_ms > max_seconds * 1000:
            # The measured length rounds up and the limit rounds down, so the two
            # numbers in this sentence can never collide. Round both the same way
            # and a 59.7 second recording against a 59.5 second cap reads "1:00
            # long, which is over the 1:00 limit" -- which is provably impossible
            # in this direction, since ceil(duration) > floor(cap) whenever
            # duration > cap.
            raise AudioTooLong(
                f"This recording is {_clock(-(-duration_ms // 1000))} long, which is over "
                f"the {_clock(int(max_seconds))} limit. Record a shorter memo.",
                duration_ms,
            )

        yield NormalizedAudio(path=destination, duration_ms=duration_ms, format=fmt)


def _require_audio_stream(source: Path) -> None:
    """
    Refuse a file that decodes fine and has nothing to transcribe in it.

    This exists because ``App\\Http\\Rules\\SniffedAudioType`` accepts real video
    files, and says so: an audio-only WebM and an audio-only MP4 are the same
    containers as the video ones with no video track, libmagic reads the
    container, so the allowlist that admits every genuine Chrome and Safari
    recording admits a screen capture too. That rule points at this module as the
    thing that "takes the audio stream and discards the rest" -- which is true
    right up until there is no audio stream.

    Without this check that case reached ffmpeg, which exited 234 with ``Output
    file does not contain any stream``, and the row then said the recording could
    not be *decoded*. It decoded perfectly. Somebody who uploaded a silent screen
    recording would have gone looking for a corrupt file.

    One extra ffprobe per voice memo, about 10 ms against a job of several
    hundred. Checked on the cases that matter rather than matched against
    ffmpeg's stderr, which is version- and locale-dependent: a video-only MP4
    returns empty with exit 0, a Chrome WebM with no duration returns ``0``, and
    a corrupt or empty file exits non-zero and is reported as undecodable by
    :func:`_run` -- which is the right answer for those.
    """
    streams = _run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(source),
        ],
        PROBE_TIMEOUT_SECONDS,
        _UNDECODABLE,
    )

    if not streams.strip():
        raise AudioError("This file has no audio track, so there is nothing to transcribe.")


def _ffmpeg_command(source: Path, destination: Path, fmt: AudioFormat) -> list[str]:
    """
    The transcode. Every flag here is load-bearing.

    ``-nostdin`` because ffmpeg reads stdin for keyboard commands by default and
    would eat the worker's. ``-vn`` and ``-map_metadata -1`` because a container
    can carry cover art and arbitrary tags, and neither belongs in a derived file
    handed to a transcription API. ``-y`` because the destination is in a fresh
    temporary directory and a prompt would be a hang.

    ``-ar``/``-ac`` here rather than in :class:`AudioFormat`, so that adding a
    third format cannot produce a stereo or 44.1 kHz one by omission.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(source),
        "-vn",
        "-map_metadata", "-1",
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        *fmt.codec_args,
        str(destination),
    ]


def _probe_duration_ms(audio: Path) -> int:
    """
    Ask ffprobe how long the normalized file is.

    ``format=duration`` -- the container's answer, not a stream's. For Ogg Opus
    that is derived from the final granule position and for WAV from the data
    chunk size, and both are written by the ffmpeg run that just finished, so
    neither can be the ``N/A`` this module exists to route around.

    Accurate to a frame. Opus codes in 20 ms frames, so the last one is padded
    and the reported duration rounds *up* by less than that -- measured, a 7.300
    second source normalized to 7.3135 as Opus and to exactly 7.300 as WAV. The
    consequence, stated rather than discovered at the boundary: the cap is strict
    by up to 20 ms on the Opus path, which is 0.003 percent at 600 seconds and
    two orders of magnitude finer than the one-second resolution the recorder's
    own timer offers.
    """
    output = _run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ],
        PROBE_TIMEOUT_SECONDS,
        _UNMEASURABLE,
    )

    try:
        seconds = float(output.strip())
    except ValueError:
        # `N/A` reaches here on a zero-exit probe of a file with no duration. It
        # should be unreachable on a file ffmpeg just wrote, and it is checked
        # anyway because the alternative is a ValueError traceback and the
        # generic "unexpected worker error" sentence on the row.
        raise AudioError(_UNMEASURABLE) from None

    if seconds < 0:
        raise AudioError(_UNMEASURABLE)

    return int(round(seconds * 1000))


def _run(command: list[str], timeout: float, failure_message: str) -> str:
    """
    Run one ffmpeg-family command, or raise :class:`AudioError` saying what broke.

    The stderr never reaches the caller's message, and that is this function's
    main job. ffmpeg writes things like ``[matroska,webm @ 0xaaaadff92d60] EBML
    header parsing failed`` and ``Error opening input`` alongside the full
    container path of the file -- and ``last_error`` is part of the API's
    response projection, so it renders in the browser. Same rule
    memo_ai/pipeline.py already applies to unclassified exceptions: the detail
    goes to the log, and the row gets a sentence this code wrote.
    """
    try:
        # A fixed argv and no shell. The only value that comes from outside this
        # module is a path, and it arrives as one element of the list rather than
        # interpolated into a string, so a filename cannot become a flag.
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        # The binary is absent, which means this is not the image ai/Dockerfile
        # builds. An operator message rather than a user one, because no memo the
        # user could record would fix it -- but it still goes on the row, since a
        # memo stuck with no explanation is worse.
        raise AudioError(
            f"Audio normalization is unavailable: {command[0]} is not installed in this image."
        ) from None
    except subprocess.TimeoutExpired:
        log.warning("%s exceeded %.0fs and was killed", command[0], timeout)

        raise AudioError(
            "This recording took too long to process and was stopped. It may be corrupt."
        ) from None

    if completed.returncode != 0:
        log.warning(
            "%s exited %d: %s",
            command[0],
            completed.returncode,
            _first_line(completed.stderr),
        )

        raise AudioError(failure_message)

    return completed.stdout


def ffmpeg_available() -> bool:
    """
    Whether both binaries are on PATH. Called once at boot, for the log line only.

    Not a refusal to start. The worker also serves text memos, which never reach
    this module, and MEMO-08's rule -- established by ``UnimplementedStt`` -- is
    that a missing capability fails the memo that needs it rather than the boot
    that might not. A restart loop would take the queue down with it.
    """
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _clock(seconds: int) -> str:
    """
    Whole seconds as ``m:ss``, matching the recorder's own timer.

    Deliberately the same shape as ``formatElapsed`` in
    web/src/components/MemoRecorder.vue, so the length quoted in a refusal is the
    length the user watched while recording, in the format they watched it in. No
    hours field there and none here: a cap of ten minutes means 61 minutes reads
    ``61:04`` rather than wrapping to ``1:04``.

    Takes seconds rather than milliseconds on purpose -- which of the two ways to
    round is correct depends on whether the number is a measurement or a limit,
    and that is the caller's question, not this function's.
    """
    return f"{seconds // 60}:{seconds % 60:02d}"


def _first_line(stderr: str) -> str:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]

    return lines[0] if lines else "no stderr"

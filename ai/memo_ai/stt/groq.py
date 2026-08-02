"""
Whisper on Groq's LPUs: the same model this stack runs locally, roughly 145x faster.

**This is the first provider in this project that sends a user's recording off the
machine, and that is the only reason it is not the default.** Everything else here
runs in the container with no key and no network; `STT_PROVIDER=groq` is opt-in,
`local` stays the shipped default, and README.md states plainly what leaves the
machine when somebody turns this on. Nothing about this module changes what a
clean `docker compose up` does.

**What it buys is latency, not accuracy.** Groq serves `whisper-large-v3-turbo` --
the same weights memo_ai/stt/local.py loads -- so the transcript is the one this
app already produces. What changes is the clock: the local model runs at 38.4
seconds of CPU per minute of audio (measured, MEMO-22), and Groq's published
figure is 217-228x realtime, about 0.26 seconds per audio-minute. On the longest
memo this app accepts that is six minutes of a worker's CPU against a couple of
seconds of somebody else's.

**What it does not buy is language detection**, which is worth stating because it
is the first thing anyone asks. The table in db/migrations/005_memo_language.sql
measured nine approaches on one 2.76-second Romanian memo and none of them
returned `ro`; `whisper large-v3-turbo` -- this model -- answered `ru` at 0.19
confidence. Hosting it changes where it runs, not what it knows. The `language`
column is still the answer.

**No new dependency.** One multipart POST does not justify a package: this module
is `urllib` from the standard library, so `STT_PROVIDER=groq` works in an image
built before it existed. The same reasoning memo_ai/costs.py applies to table
formatting.

**Failures fall back rather than fail the memo.** Every way this provider can be
unusable -- no key, a rate limit, a 500, a timeout -- raises
:class:`~memo_ai.stt.base.SttUnavailable`, which memo_ai/stt/chain.py answers by
trying ``STT_FALLBACK``. So the recommended configuration is
``STT_PROVIDER=groq`` with the shipped ``STT_FALLBACK=local``: Groq when it can,
the local model when it cannot, and ``memos.stt_provider`` recording which one
actually produced the words.
"""

import json
import logging
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from memo_ai import failures
from memo_ai.stt.base import SttError, SttUnavailable, Transcript

log = logging.getLogger(__name__)

# Groq's transcription endpoint, which is OpenAI-shaped on purpose -- the path
# says so. That is what makes this module short, and it is also why a reader
# should not assume the two are interchangeable everywhere else: the *request* is
# the same multipart form, the model names are not (`whisper-large-v3-turbo` here
# against faster-whisper's `large-v3-turbo`), which is why GROQ_STT_MODEL is its
# own variable rather than a reuse of STT_MODEL.
ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"

# The model, repeated from docker-compose.yml like every other default in this
# package. Turbo rather than `whisper-large-v3`: same accuracy on everything this
# app has been run against, roughly a third of the price ($0.04 against $0.111 per
# audio-hour), and it is the model the local provider already defaults to, so
# switching providers does not also switch models.
DEFAULT_MODEL = "whisper-large-v3-turbo"

# How long one request may take, end to end.
#
# Generous against what this should cost. The audio is Opus at 24 kbps -- this
# class asks for no `audio_format`, so memo_ai/audio.py gives it the default, and
# that default exists for exactly this caller: a full 600-second memo is about 1.8
# MB, against 19.2 MB for the WAV the local provider takes. So the terms are an
# upload of a couple of megabytes and about three seconds of inference.
#
# Two minutes covers that on a connection slow enough to be painful, and it is far
# below the local provider's own worst case (300s of model load plus a 2,400s
# decode deadline), which is what keeps `pipeline.job_budget_seconds` a true bound
# without adding a term for this provider -- see the note there.
REQUEST_TIMEOUT_SECONDS = 120.0

# What Groq accepts in one request. 25 MB is the free tier's limit; paid tiers are
# higher, and taking the lower number means this refuses before the network does.
#
# Not reachable on the shipped configuration: Opus at the 600-second cap is about
# 1.8 MB, an order of magnitude under. It is checked anyway because the one way to
# get here is somebody raising MAX_AUDIO_SECONDS, and a refusal that names the size
# is a better answer than a 413 whose body is somebody else's HTML.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Every sentence below can be written to `memos.last_error`, which the API projects
# to the browser -- so the same rule SttError states applies: something a person can
# act on, and no keys, no URLs, no internal paths. In particular none of them
# include the response body, which is a third party's text rather than one this
# project wrote.
_NO_KEY = (
    "GROQ_API_KEY is not set, so the Groq transcription provider cannot be used. "
    "Set it, or set STT_PROVIDER=local to transcribe on this machine."
)

_REJECTED_KEY = (
    "Groq rejected the configured API key. Check GROQ_API_KEY, or set "
    "STT_PROVIDER=local to transcribe on this machine."
)

_RATE_LIMITED = (
    "Groq's rate limit was reached, so this memo was not transcribed there. The "
    "free tier allows 2,000 recordings a day."
)

_UNAVAILABLE = "Groq could not be reached. This memo will be tried again."

_SERVICE_ERROR = "Groq returned an error and did not transcribe this memo."

_TOO_LARGE = (
    "This recording is too large for Groq to accept in one request. Lower "
    "MAX_AUDIO_SECONDS, or set STT_PROVIDER=local."
)

_REFUSED = "Groq could not read this recording."

_NO_SPEECH = "No speech was found in this recording."

_BAD_ANSWER = "Groq returned a response this build could not read."


class GroqStt:
    """
    One HTTP request per memo. No model in this process, no weights on disk.

    Constructed at boot like every other provider and just as cheaply -- **a
    missing key is not a boot failure**, which is the rule
    memo_ai/stt/unimplemented.py set and the reason it matters here: the API key
    is the one thing this provider needs and the one thing a person will forget.
    Failing to start would turn that into a `restart: unless-stopped` loop that
    also takes down text memos, so an absent key fails the first *voice memo*
    instead -- as :class:`SttUnavailable`, which walks the fallback chain to the
    local model.

    No ``audio_format``, deliberately. That means Opus, which memo_ai/audio.py
    documents as the format a hosted provider must use, and this is the caller
    that comment was written for.
    """

    name = "groq"

    def __init__(self, api_key: str | None, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self.model = model

    def transcribe(self, source: Path, language: str | None = None) -> Transcript:
        """
        Send one normalized recording to Groq and return what came back.

        ``language`` is passed through when the memo names one, and omitted when it
        does not -- the same three-source precedence the local provider applies,
        minus the deployment-wide default, which belongs to whichever provider is
        loading a model. Naming it here does the same thing it does locally: skips
        a detection pass, and removes the chance of the wrong guess that
        db/migrations/005_memo_language.sql exists because of.
        """
        if not self._api_key:
            # Checked before the file is read, so a deployment that never set the
            # key spends nothing discovering it on every memo.
            raise SttUnavailable(_NO_KEY)

        audio = source.read_bytes()

        if len(audio) > MAX_UPLOAD_BYTES:
            # SttError and not SttUnavailable: the fallback is fed the same file, so
            # walking the chain over a size limit would only reach a provider with
            # its own. `local` has none, but it is the *file* that is wrong for this
            # provider, and memo_ai/stt/chain.py reserves the unavailable branch for
            # a provider that cannot run at all.
            log.warning("groq: %s is %d bytes, over the %d limit", source.name, len(audio), MAX_UPLOAD_BYTES)

            raise SttError(_TOO_LARGE)

        # Around the request rather than inside it, and the distinction is worth
        # stating because `stt_ms` means something different for this provider than
        # for the local one. Locally it is CPU time; here it is a round trip --
        # upload, queue, inference, response. That is the honest number for "how
        # long did this memo wait", and it is not comparable to the local figure in
        # memo_ai/costs.py's median, which is why `stt_provider` is on the row.
        started = time.monotonic()
        payload = self._post(_multipart(audio, source.name, self.model, language))
        elapsed_ms = round((time.monotonic() - started) * 1000)

        text = payload.get("text") if isinstance(payload, dict) else None

        if not isinstance(text, str):
            log.warning("groq: response had no usable text field: %r", type(payload).__name__)

            raise SttError(_BAD_ANSWER)

        text = text.strip()

        if not text:
            # The same classification the local provider gives an empty decode, and
            # for the same reason: coded so the app can discard a recording with
            # nothing in it rather than leaving a card that says the user said
            # nothing. memo_ai/failures.py has the argument.
            raise SttError(_NO_SPEECH, code=failures.NO_SPEECH)

        # No `cost_micro_usd`, and that is a statement rather than an omission.
        # Groq's response reports no price, so there is nothing measured to record --
        # and on the free tier the true answer is zero anyway. memo_ai/rates.py
        # carries the per-audio-hour figure that lets MEMO-22's report project it,
        # which is the split that file exists to keep: measurements on the row,
        # prices in code.
        return Transcript(
            text=text,
            provider=self.name,
            model=self.model,
            inference_ms=elapsed_ms,
        )

    def _post(self, body: tuple[bytes, str]) -> object:
        """
        The request, with every failure mapped onto the two classes the chain reads.

        The mapping is the whole of this method and it follows one question: *would
        the fallback provider do better?* Everything about Groq being unreachable,
        throttled, broken or misconfigured says yes, so it is
        :class:`SttUnavailable` and memo_ai/stt/chain.py transcribes locally
        instead. Only a refusal *of this recording* is terminal, because the
        fallback is handed the same bytes.
        """
        content, content_type = body
        request = urllib.request.Request(
            ENDPOINT,
            data=content,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise _classified(error.code) from None
        except urllib.error.URLError as error:
            # DNS, TLS, connection refused, and the timeout -- all of them arrive
            # here, and all of them mean the same thing to the chain. `error.reason`
            # is logged and deliberately kept out of the sentence: it can contain
            # the resolved host and the proxy in the way.
            log.warning("groq: request failed: %s", error.reason)

            raise SttUnavailable(_UNAVAILABLE) from None
        except json.JSONDecodeError:
            # A 200 whose body is not JSON. Reachable through a captive portal or a
            # corporate proxy that answers with a login page, which is a network
            # problem wearing a success code.
            log.warning("groq: response was not JSON")

            raise SttUnavailable(_UNAVAILABLE) from None


def _classified(status: int) -> SttError:
    """
    One HTTP status, as the exception the pipeline knows what to do with.

    A function rather than a dict, so each branch can carry the reason it is on the
    side of the line it is on.
    """
    if status in (401, 403):
        # Unavailable rather than terminal even though a bad key will not fix
        # itself. What makes that right is the chain: this is precisely the case
        # where falling back to the local model is the behaviour a person wants,
        # and the sentence tells them what to fix.
        return SttUnavailable(_REJECTED_KEY)

    if status == 429:
        return SttUnavailable(_RATE_LIMITED)

    if status == 413:
        # Terminal. Groq measured the same file this build did and disagreed about
        # the limit; the fallback gets the same bytes, so there is nothing to gain.
        return SttError(_TOO_LARGE)

    if status >= 500:
        return SttUnavailable(_SERVICE_ERROR)

    if status == 400:
        # The audio itself. memo_ai/audio.py already transcoded it, so this should
        # be unreachable -- and it is terminal for the reason AudioError gives:
        # every provider in the chain is fed the same normalized file.
        return SttError(_REFUSED, code=failures.UNREADABLE)

    # Any other 4xx is a request this build constructed wrongly, which is a bug
    # rather than an outcome. Unavailable so the memo still gets transcribed
    # locally while somebody reads the log.
    log.warning("groq: unexpected HTTP %d", status)

    return SttUnavailable(_SERVICE_ERROR)


def _multipart(audio: bytes, filename: str, model: str, language: str | None) -> tuple[bytes, str]:
    """
    The request body, encoded by hand.

    ``multipart/form-data`` is twenty lines of byte concatenation and the only
    reason this module would otherwise need `requests` or `httpx` in
    requirements.txt. That trade is not close: a new dependency in the ai image
    costs a rebuild, and this project's images carry 2.8 GB of baked model weights.

    The boundary is a fresh UUID per request rather than a constant, which is the
    one part of this that is not merely mechanical: a boundary that appeared inside
    the audio bytes would split the part and corrupt the upload. Opus is compressed
    binary, so a fixed string would be unlikely rather than impossible, and
    "unlikely" is not a property to give a body assembled from user recordings.
    """
    boundary = f"----memo{uuid.uuid4().hex}"
    # Guessed from the suffix, which memo_ai/audio.py owns: `.opus` by default,
    # `.wav` if a provider asked. Groq reads the filename as a format hint, so
    # sending the real suffix matters more than the type header.
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    fields = {"model": model, "response_format": "json"}

    if language:
        # Omitted rather than sent empty when nobody chose one. An empty `language`
        # is not the same request as no `language`, and only the second one asks
        # the model to detect.
        fields["language"] = language

    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

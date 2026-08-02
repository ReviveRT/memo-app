"""
Fetching a recording from an S3-compatible bucket.

**Why this exists.** ``memos.audio_path`` holds a storage *key*, and until now the only
thing that key ever meant was a path under ``AUDIO_DIR`` on a volume both containers
mount. A free deployment has no such volume -- the disk is rebuilt on every deploy -- so
the API grew App\\Storage\\S3AudioStorage and writes recordings to a bucket instead. This
is the other half of that: without it a worker pointed at the same database claims a voice
memo, looks for a file that was never written to any volume, and fails it with "missing
from the audio volume" -- which is true and completely misleading.

**Why stdlib rather than boto3.** The same argument ``memo_ai/stt/groq.py`` makes for
using ``urllib`` there: this package installs into an image alongside faster-whisper and
llama-cpp-python, and a dependency added for one optional code path is one every build
pays for. What that costs is signing requests by hand, which is the part of this file
worth reviewing carefully -- and which is verified against a real S3 implementation rather
than reasoned about, because a signature is either byte-exact or it is a 403.

Only reads are implemented. The worker never writes a recording and never deletes one:
the API owns the object's whole lifetime, which is also why there is no bucket
configuration on this side beyond what it takes to GET.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from memo_ai.config import Settings

log = logging.getLogger(__name__)

# Seconds to wait for the bucket. Generous next to the API's own timeouts, because this
# transfers a whole recording rather than exchanging a few hundred bytes, and the worker
# has nothing better to do while it arrives -- it has already claimed the row and the
# reaper's lease covers it.
REQUEST_TIMEOUT_SECONDS = 60

# How long the signed URL stays valid, and deliberately the same number rather than a
# second knob. S3 checks the signature when the request *arrives*, not while the body is
# streaming, so this only has to outlive the gap between signing and connecting -- which is
# microseconds here, since the URL is built and used in the same function and never leaves
# it. Tying it to the socket timeout keeps one number to reason about, and the property
# that matters is that a URL cannot outlive the call that made it.
URL_EXPIRY_SECONDS = REQUEST_TIMEOUT_SECONDS

_ALGORITHM = "AWS4-HMAC-SHA256"


class BlobError(RuntimeError):
    """A recording could not be fetched. Carries a sentence fit to store on the row."""


@contextmanager
def fetched(settings: Settings, key: str) -> Iterator[Path]:
    """
    Download one object to a temporary file and yield its path.

    A context manager yielding a path, so the caller's code is identical whether the
    recording came off a volume or out of a bucket -- ``pipeline.owed_audio`` hands either
    to ``audio.normalize`` without knowing which. The temporary file is removed on the way
    out, including when the caller raises.

    ``/tmp`` on the container's writable layer, deliberately, and not ``AUDIO_DIR``. On a
    bucket deployment that directory may not exist at all, and where it does it is the
    volume this function exists to avoid depending on.
    """
    url = _signed_url(settings, key)

    with tempfile.TemporaryDirectory(prefix="memo-blob-") as scratch:
        destination = Path(scratch) / Path(key).name

        request = urllib.request.Request(url, method="GET")

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                with destination.open("wb") as handle:
                    # copyfileobj rather than response.read(): a recording is capped at 12
                    # MiB by the API, which is small enough to hold in memory and large
                    # enough that there is no reason to.
                    shutil.copyfileobj(response, handle)
        except urllib.error.HTTPError as error:
            # 404 is the interesting one and is kept distinct: it means the row references
            # an object that is not in the bucket, which is the bucket equivalent of the
            # missing-file case and is not a transient fault worth retrying forever.
            if error.code == 404:
                raise BlobError(
                    "The audio file for this memo is missing from the bucket."
                ) from error

            # Everything else -- 403 from wrong credentials, 500 from the provider -- is a
            # deployment fault. The status is included because it is the one part worth
            # telling apart; the body is not, because it is XML nobody reading a memo card
            # can act on.
            raise BlobError(
                f"The audio bucket refused the request for this memo (HTTP {error.code})."
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise BlobError("The audio bucket could not be reached.") from error

        log.info("fetched %s from the bucket (%d bytes)", key, destination.stat().st_size)

        yield destination


def _signed_url(settings: Settings, key: str) -> str:
    """
    A presigned GET, valid for as long as one download should take.

    Query-string signing rather than an Authorization header, because the two are the same
    arithmetic and this one keeps every signed value in the URL -- so a failure can be
    reproduced with curl, which for hand-written signing is the difference between a bug
    that is findable and one that is not.
    """
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    endpoint = settings.audio_bucket_endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.netloc

    # Path style: /{bucket}/{key}. The alternative is virtual-hosted style, where the
    # bucket is a subdomain -- which R2 does not offer on its S3 endpoint and which would
    # need the host to be rebuilt rather than taken from the endpoint. One shape, and it
    # is the one both providers accept.
    #
    # safe="/" so key separators survive, everything else percent-encoded. An unencoded
    # space in a key is a malformed request rather than a signature mismatch, which is a
    # much harder error to read.
    canonical_uri = "/" + urllib.parse.quote(
        f"{settings.audio_bucket}/{key}".lstrip("/"), safe="/"
    )

    credential = f"{settings.audio_bucket_key}/{date_stamp}/{settings.audio_bucket_region}/s3/aws4_request"

    # Sorted by key, and that is required rather than tidy: the canonical query string is
    # part of what gets signed, and S3 rebuilds it in sorted order on its side.
    query = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(URL_EXPIRY_SECONDS),
        "X-Amz-SignedHeaders": "host",
    }

    # quote_via=urllib.parse.quote with safe="" so that the slashes inside X-Amz-Credential
    # are encoded as %2F. urlencode's default quote_plus would encode spaces as "+", which
    # SigV4 does not accept.
    canonical_query = urllib.parse.urlencode(
        sorted(query.items()), quote_via=urllib.parse.quote, safe=""
    )

    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_query,
            f"host:{host}\n",
            "host",
            # UNSIGNED-PAYLOAD, which is what a presigned URL uses: the signature is
            # computed before the request is made and so cannot cover a body. A
            # header-signed GET would put the SHA-256 of the empty string here instead.
            "UNSIGNED-PAYLOAD",
        ]
    )

    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            f"{date_stamp}/{settings.audio_bucket_region}/s3/aws4_request",
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(settings.audio_bucket_secret, date_stamp, settings.audio_bucket_region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return f"{parsed.scheme}://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    """
    The four-step HMAC chain SigV4 derives a per-day, per-region, per-service key with.

    Each step keys the next, which is the whole point: the key that signs a request is
    useless for a different day, region or service, so a signature captured off the wire
    cannot be replayed against anything else.
    """
    key = f"AWS4{secret}".encode()

    for part in (date_stamp, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()

    return key

"""
MEMO-15: the weights are in the image, and the worker looks there first.

Two halves, and they are separated because only one of them can run everywhere.

The resolver tests drive :func:`_model_source` against a temporary directory, so
they run on a clean clone with no image built and no model on the machine. What
they check is the decision -- baked path, or fall through to the library -- which
is this package's, not faster-whisper's.

The image tests check that the bake actually happened, and they can only do that
from inside a container built from ai/Dockerfile. They skip elsewhere. That is a
weaker guarantee than the ones above and it is placed where the difference is
visible, rather than folded into a single file that passes for two unrelated
reasons.
"""

import os
from pathlib import Path

import pytest

from memo_ai.config import DEFAULT_STT_MODEL
from memo_ai.stt import local
from memo_ai.stt.local import BAKED_MODEL_DIR, DETECT_MODEL, _model_source

# The first four bytes of every GGUF file, little-endian, as the format's own
# specification defines them. Checked rather than the file's size, because the
# failure this guards against does not change the size in a way worth asserting
# on: a proxy that answers HTML to an unauthenticated fetch writes a plausible
# number of bytes, and `hf_hub_download` will happily save them.
GGUF_MAGIC = b"GGUF"


def _bake(root: Path, size: str) -> Path:
    """A directory that looks like one `download_model(output_dir=...)` left."""
    target = root / size
    target.mkdir(parents=True)
    (target / "model.bin").write_bytes(b"not really a model")
    (target / "tokenizer.json").write_text("{}")

    return target


def test_a_baked_model_resolves_to_its_directory(tmp_path, monkeypatch):
    baked = _bake(tmp_path, "large-v3-turbo")
    monkeypatch.setattr(local, "BAKED_MODEL_DIR", tmp_path)

    assert _model_source("large-v3-turbo") == str(baked)


def test_an_unbaked_model_resolves_to_its_name(tmp_path, monkeypatch):
    """
    The supported way to run a model nobody baked, not a degraded mode.

    `STT_MODEL=medium` on an image built with the defaults has to keep working --
    faster-whisper resolves the name against HuggingFace and writes it into the
    `model-cache` volume, exactly as every model did before this task.
    """
    _bake(tmp_path, "large-v3-turbo")
    monkeypatch.setattr(local, "BAKED_MODEL_DIR", tmp_path)

    assert _model_source("medium") == "medium"


def test_an_empty_baked_directory_is_not_treated_as_baked(tmp_path, monkeypatch):
    """
    The case the check exists for, and the reason it looks at `model.bin`.

    A build whose fetch died after mkdir leaves the directory behind. Handing an
    empty one to CTranslate2 raises `RuntimeError: Unable to open file 'model.bin'
    in model '...'` out of C++ -- checked against the real library, not assumed --
    which memo_ai/stt/local.py can only classify as a generic engine failure. The
    memo would fail with a sentence about the engine rather than downloading the
    weights and working.
    """
    (tmp_path / "large-v3-turbo").mkdir()
    monkeypatch.setattr(local, "BAKED_MODEL_DIR", tmp_path)

    assert _model_source("large-v3-turbo") == "large-v3-turbo"


def test_the_resolver_reports_which_path_it_took(tmp_path, monkeypatch, caplog):
    """
    "Did this container reach the network for weights" has to be answerable from
    the log, because the alternative is inspecting an image to explain a slow
    first memo.
    """
    _bake(tmp_path, "tiny")
    monkeypatch.setattr(local, "BAKED_MODEL_DIR", tmp_path)

    with caplog.at_level("INFO", logger=local.__name__):
        _model_source("tiny")
        _model_source("medium")

    baked, downloaded = caplog.messages[-2:]

    assert "baked" in baked
    assert "HuggingFace" in downloaded


# --- Inside the image --------------------------------------------------------

needs_image = pytest.mark.skipif(
    not BAKED_MODEL_DIR.is_dir(),
    reason=f"needs an image built from ai/Dockerfile ({BAKED_MODEL_DIR} is absent)",
)


@needs_image
@pytest.mark.parametrize("size", [DEFAULT_STT_MODEL, DETECT_MODEL])
def test_the_shipped_models_are_baked_into_the_image(size):
    """
    Both of them, and the detector is the one worth naming.

    STT_LANGUAGE ships empty, so the default configuration detects per recording
    and loads `tiny` alongside the real model. An image that baked only
    DEFAULT_STT_MODEL still opens a socket on the first voice memo, for 78 MB, and
    the offline criterion fails on a detail nobody would think to look for.
    """
    assert _model_source(size) == str(BAKED_MODEL_DIR / size)


@needs_image
def test_the_baked_weights_are_read_only_to_the_worker():
    """
    Root-owned and world-readable. The worker runs as `memo` and only opens them,
    which is what makes "the weights cannot drift at runtime" a property of the
    filesystem rather than of the code.

    Asserted on the mode bits rather than through ``os.access``, and that is not a
    stylistic preference -- the first version of this test used ``os.access`` and
    failed. README.md's documented invocation for this suite is ``--user 0:0``,
    because pip has to write into site-packages, and root bypasses permission bits
    entirely: ``os.access(weights, os.W_OK)`` is true for root on a 0444 file. So
    the question ``os.access`` answers is "may *this* process write it", which is
    not the question. The mode is the same under every uid.
    """
    weights = BAKED_MODEL_DIR / DEFAULT_STT_MODEL / "model.bin"
    info = weights.stat()

    # Owned by root, and the worker is not root -- ai/Dockerfile's `USER memo`.
    assert info.st_uid == 0

    # World-readable, and writable by nobody but its owner.
    assert info.st_mode & 0o004
    assert not info.st_mode & 0o022


@needs_image
def test_the_enrichment_weights_are_baked_and_are_a_real_gguf():
    """
    MEMO-21 writes the enricher; this only proves its weights arrived.

    The magic-byte check is not ceremony. `hf_hub_download` against a repository
    or filename that has moved does not always fail loudly, and a file of roughly
    the right size that starts with anything else is a failure MEMO-21 would
    inherit as an unexplained llama.cpp error months from now.

    The variable is asserted rather than indexed. `ENRICH_MODEL_PATH` is set by
    ai/Dockerfile and is the only thing telling MEMO-21 where to look, so an image
    that baked the weights and lost the variable is a real defect -- and a bare
    `os.environ[...]` reports it as a KeyError traceback, which reads like a
    broken test rather than a broken image.
    """
    configured = os.environ.get("ENRICH_MODEL_PATH")

    assert configured, "ai/Dockerfile must set ENRICH_MODEL_PATH; MEMO-21 reads it"

    path = Path(configured)

    assert path.is_file()

    with path.open("rb") as handle:
        assert handle.read(4) == GGUF_MAGIC

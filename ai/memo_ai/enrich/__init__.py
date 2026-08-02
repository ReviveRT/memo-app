"""
Name to enricher. The registry, and the only place that knows what names exist.

The same job ``memo_ai/stt/__init__.py`` does for transcription, and the set below
has the same obligation: it has to agree with the ``ENRICH_PROVIDER`` comment in
.env.example, the variable table in README.md, and the default in
docker-compose.yml. A name documented there and unknown here is a refusal to boot
against the repo's own advice.

Two names, and no chain. ``STT_FALLBACK`` exists because a failed transcription is
worth a second opinion from a different provider; a failed *enrichment* is not,
because there is no second enricher and because the outcome of giving up is a
``ready`` memo with its transcript, which is a perfectly good memo. That
asymmetry is stated at :class:`~memo_ai.enrich.base.EnrichmentError`, which has no
``EnrichmentUnavailable`` counterpart for the same reason.

This module re-exports the contract so that ``from memo_ai.enrich import
Enrichment`` keeps meaning what it did when this package was a single file --
memo_ai/pipeline.py, memo_ai/memos.py and the test doubles all import it that way.
"""

from memo_ai.config import ConfigError, Settings
from memo_ai.enrich.base import (
    NO_ENRICHMENT,
    Enricher,
    Enrichment,
    EnrichmentError,
    NoEnrichment,
    Usage,
)
from memo_ai.enrich.local import LocalLlmEnricher

__all__ = [
    "NO_ENRICHMENT",
    "PROVIDER_NAMES",
    "Enricher",
    "Enrichment",
    "EnrichmentError",
    "LocalLlmEnricher",
    "NoEnrichment",
    "Usage",
    "require_known",
    "resolve",
]

PROVIDER_NAMES = frozenset({LocalLlmEnricher.name, NoEnrichment.name})


def resolve(settings: Settings) -> Enricher:
    """
    Build the enricher ``ENRICH_PROVIDER`` names, or refuse to start.

    Nothing here is expensive, and that is a rule rather than an observation.
    :class:`LocalLlmEnricher` opens no file and loads no weights until the first
    memo that needs enriching, so a worker draining a queue of text memos never
    pays for the model -- and, more to the point, a bad ``ENRICH_MODEL_PATH`` fails
    one memo's *summary* instead of turning ``restart: unless-stopped`` into a
    restart loop that also stops transcription. Enrichment may not fail a memo;
    still less may it fail the boot.

    ``none`` is a supported configuration rather than a degenerate one. It is the
    way to run this stack on a machine that cannot spare the memory for a second
    model, and it is what the shipped stack did before MEMO-21 -- memos still
    transcribe, store and search, and get the heuristic title
    ``memo_ai/titles.py`` cuts from the transcript.
    """
    if settings.enrich_provider == NoEnrichment.name:
        return NO_ENRICHMENT

    if settings.enrich_provider == LocalLlmEnricher.name:
        return LocalLlmEnricher(settings.enrich_model_path)

    raise _unknown("ENRICH_PROVIDER", settings.enrich_provider)


def require_known(variable: str, name: str) -> None:
    """Validate a provider name without building it."""
    if name not in PROVIDER_NAMES:
        raise _unknown(variable, name)


def _unknown(variable: str, name: str) -> ConfigError:
    allowed = ", ".join(sorted(PROVIDER_NAMES))

    return ConfigError(f"{variable} must be one of {allowed}, got {name!r}.")

"""
The enrichment contract: a transcript in, a title and a summary and some tags out.

No real implementation here, and that is the point of the file rather than an
omission. memo_ai/enrich/local.py is the enricher MEMO-21 wrote; this is the
*seam* it plugs into, and the seam predates it because the second of MEMO-16's two
commit points is defined by what happens around this call:

  * enrichment returns something -> ``title``, ``summary``, ``tags``, ``category``
    and ``enriched_at`` are written, and the memo is ``ready``.
  * enrichment raises -> ``enrichment_error`` is written, and the memo is **still**
    ``ready``, carrying the transcript it already has and a fallback title.

That second line is the whole reason enrichment is not allowed to be a plain
function call inside the transcription try-block. Enrichment is best-effort;
transcription is not. A memo whose summary could not be generated is a memo with a
transcript, and ``failed`` would be a lie about it. memo_ai/pipeline.py holds both
halves and db/migrations/001_init.sql says the same thing at
``enrichment_error``'s definition.

The shape mirrors memo_ai/stt/base.py deliberately -- a Protocol, a frozen result,
and one classified exception whose message is safe to show a person -- so that a
reader who has understood the STT seam has understood this one. The one difference
is the null implementation below, which exists because "no enricher" is a supported
configuration here and "no transcriber" is not.
"""

from dataclasses import dataclass
from typing import Protocol

# No logger in this module, deliberately. Nothing here decides anything -- the
# contract, the result and the null implementation are all declarations -- and the
# one place an enrichment outcome is judged is memo_ai/pipeline.py's `_enriched`,
# which logs it there. A logger here would only ever be used by an implementation,
# and an implementation should own its own.


class EnrichmentError(Exception):
    """
    An enrichment attempt that produced nothing usable.

    ``str()`` of one of these is written to ``memos.enrichment_error``, which is a
    column the API can project to the browser -- so the same rule ``SttError``
    states applies without change: a sentence a person can act on, and no keys, no
    connection strings, no internal paths. memo_ai/pipeline.py enforces the other
    half by refusing to copy an *unclassified* exception's text onto the row.

    No ``EnrichmentUnavailable`` counterpart, unlike ``SttUnavailable``. That
    distinction exists so a fallback chain can tell "try the other provider" from
    "the audio is the problem", and there is no enrichment chain: an enricher that
    cannot run and an enricher that produced nothing lead to exactly the same
    place, which is a ``ready`` memo with an untouched transcript.
    """


@dataclass(frozen=True)
class Usage:
    """
    What one enrichment consumed. MEMO-22's half of the result.

    Separate from :class:`Enrichment` rather than five more fields on it, and the
    reason is :meth:`Enrichment.is_empty`. That method decides whether
    ``enriched_at`` is stamped, and it must answer "did this produce anything to
    show a person" -- a run that burned 900 tokens and returned no usable field
    has *not* enriched the memo. Flat fields would make that a rule somebody has
    to remember; a nested object makes it obvious, because ``is_empty`` never
    looks in here.

    **Tokens rather than dollars, deliberately.** An enricher reports what it
    consumed; what that is worth is a rate table's job, and memo_ai/rates.py is
    where the rate lives precisely so the row keeps a measurement that stays true
    when prices move. The same split :class:`~memo_ai.stt.base.Transcript` makes,
    from the other side: a hosted provider that reports a charge fills in
    ``cost_micro_usd`` there, and everything local reports usage and no money.

    ``inference_ms`` is the generation alone and excludes the lazy model load, for
    the reason ``Transcript.inference_ms`` excludes whisper's: the load is a cost
    of the process and would make the first enriched memo after a boot an outlier
    in a latency figure.

    Every field is optional because an implementation may know some of them and
    not others -- a provider that reports no token counts still has a wall-clock
    time, and ``memos`` writes each column independently.
    """

    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    inference_ms: int | None = None


@dataclass(frozen=True)
class Enrichment:
    """
    What an enricher produces. Every field optional, and that is deliberate.

    A model that returned a good title and refused to guess a category has done
    most of its job, and the alternative -- requiring all four, so a missing
    category is an ``EnrichmentError`` -- would throw away the title to punish the
    category. memo_ai/memos.py writes each field with COALESCE for the same reason:
    absent means "leave the column alone", not "erase it".

    ``tags`` is a tuple rather than a list because this class is frozen and a list
    would make it unhashable and mutable through the back door -- and because an
    immutable default needs no ``field(default_factory=...)``, which a list would.
    It is converted to a Python list on the way into psycopg, which is what maps to
    ``text[]``.

    Nothing here carries a cost in dollars. Enrichment spend belongs on the row
    rather than on the result -- see ``Transcript`` for the same argument on the
    transcription side -- and what an enricher reports instead is :class:`Usage`,
    which is a measurement rather than a price.
    """

    title: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    category: str | None = None

    # What producing the four fields above consumed, or None for an enricher that
    # does not say. MEMO-22 persists it; nothing else reads it, and no behaviour
    # anywhere branches on it.
    usage: Usage | None = None

    def is_empty(self) -> bool:
        """
        Whether this carries nothing **to show a person**.

        Read by the pipeline to decide whether ``enriched_at`` is set. An enricher
        that ran and found nothing to say has not enriched the memo, and stamping
        the column anyway would make "has this memo been enriched?" unanswerable
        the day somebody wants to re-run the ones that were not.

        ``usage`` is deliberately not consulted. A generation that spent 900 tokens
        and produced no usable field is exactly the case this method has to call
        empty -- the memo has no title, no summary and no tags, whatever it cost to
        find that out -- and ``memo_ai/memos.py`` writes the accounting columns off
        ``usage`` rather than off this answer, so such a run is still recorded.

        That combination is reachable through the contract and **not** through the
        enricher this project ships: :class:`~memo_ai.enrich.local.LocalLlmEnricher`
        raises rather than returning an empty result, so its unusable answers reach
        the row as an ``enrichment_error`` with no usage beside it. See NOTES.md,
        which states that gap and why it is not worth closing while every
        generation here is free.
        """
        return not (self.title or self.summary or self.tags or self.category)


class Enricher(Protocol):
    """
    Structural, like ``SttProvider``, so a test double is a five-line class that
    imports nothing from here.
    """

    name: str

    def enrich(self, transcript: str) -> Enrichment | None:
        """
        Derive what can be derived from one transcript, or raise
        ``EnrichmentError``.

        ``None`` and an empty :class:`Enrichment` mean the same thing to the
        caller and both are legal: "nothing to add, and that is not a failure".
        The transcript is the text as it stands on the row, which for a resumed
        job is the one an *earlier* attempt committed -- so an implementation may
        not assume it just produced it.
        """
        ...


class NoEnrichment:
    """
    The enricher used when ``ENRICH_PROVIDER=none``, which is no longer the default.

    A null object rather than ``None`` and a branch at the call site, because the
    branch would have to be repeated in the pipeline, in its tests, and in whatever
    MEMO-24 adds -- and every copy of it is a chance to skip the second commit
    entirely rather than skip the enrichment inside it. The commit must still
    happen: it is what moves the row to ``ready`` and gives it its fallback title.

    So under this configuration a voice memo reaches ``ready`` with a transcript, a
    title ``memo_ai/titles.py`` cut out of that transcript, and NULL for summary,
    tags, category and ``enriched_at``. That is an accurate description of what was
    done to it, and it is the row MEMO-21's local model improves on -- at the price
    of about 1.7 GB of resident memory on the first memo that needs it, which is
    why this remains a supported way to run the stack rather than a historical
    artefact.
    """

    name = "none"

    def enrich(self, transcript: str) -> Enrichment | None:
        return None


# A module-level instance, so it can be a default argument. Safe because the class
# is stateless -- there is nothing for two callers to share and corrupt -- and it
# keeps the pipeline's signature honest: enrichment is a parameter with a default,
# not something a caller can forget to pass and thereby skip the second commit.
NO_ENRICHMENT = NoEnrichment()

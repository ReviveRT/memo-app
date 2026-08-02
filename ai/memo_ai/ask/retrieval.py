"""
Which memos a question is about, decided by Postgres rather than by a model.

**There is no vector store here and that is the design, not a shortcut.** MEMO-19
already put a GIN index over a generated ``tsvector`` on this table, and a question
is a bag of words like any other query -- so the retrieval half of "ask my memos"
is a ``SELECT``. Adding pgvector would mean an extension, an embedding model, a
backfill migration and a second thing to keep in step with the transcript column,
to rank three rows out of a table a person can scroll. NOTES.md states the trade
where the architecture is discussed.

**A question is not a search box, and that is the one real difference from
``MemoRepository::list``.** Both read the same column with the same dictionary, and
they diverge in two places that would be bugs if they were copied across:

  * ``websearch_to_tsquery`` ANDs its terms, which is right for somebody typing
    words into a filter and wrong for a sentence. "what did I say about the landing
    page" compiles to ``'say' & 'land' & 'page'``, so a memo that says "the landing
    page copy needs work" does not match -- it never said "say". :data:`_LEXEMES`
    instead asks Postgres for the question's lexemes and :data:`_SEARCH` ORs them,
    which is what makes ``ts_rank`` mean "matched more of the question".
  * there is no ILIKE arm. The list has one for partial words and run-together
    tokens, matching the query as a substring; the substring of a *sentence* is
    almost never in a memo, so here it would cost a trigram lookup per question to
    find nothing.

**The excerpt is chosen around the match, not taken off the front.** ``ts_headline``
is what does it, and it is why a ten-minute memo can be cut to
``ASK_MEMO_CHARS`` without cutting away the sentence the question was about. That
is a second thing the database already knew how to do and the reason the
prompt-side cap is a ceiling rather than the whole selection.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# The dictionary, in one place. `english` is what db/migrations/001_init.sql's
# generated column is built with, so a query in any other one would ask for lexemes
# that are not in the index -- silently, matching nothing rather than erroring.
CONFIG = "english"

# What `ts_headline` is asked for, and every option earns its place:
#
#   * StartSel and StopSel empty, because the default wraps every match in `<b>`.
#     This text goes into a prompt, not into HTML, and markup would be tokens spent
#     teaching the model that some words are special.
#   * MaxFragments greater than zero is what switches ts_headline from "the first
#     N words of the document" to "the passages that matched". It is the whole
#     reason this call is here.
#   * MinWords and MaxWords bound each fragment. Four fragments of up to 60 words
#     is around 1,400 characters, which is deliberately a little over
#     ASK_MEMO_CHARS -- the Python cap is then a real ceiling rather than a limit
#     nothing ever reaches.
#   * FragmentDelimiter says out loud that something was left out. A gap rendered as
#     a space reads as one continuous sentence the speaker never said, which is
#     exactly the kind of thing to keep out of evidence a model is about to
#     summarise.
HEADLINE_OPTIONS = (
    'StartSel="", StopSel="", '
    "MaxWords=60, MinWords=25, MaxFragments=4, "
    'FragmentDelimiter=" … "'
)

# The question's lexemes, from Postgres' own parser.
#
# Its own statement rather than a CTE inside the search below, because the two
# empty cases are different answers and one query cannot tell them apart: a
# question with no lexemes ("what about it?") and a question whose lexemes match no
# memo both come back as zero rows. The first is worth saying "ask about something
# in particular" to and the second is worth saying "nothing mentions that" to, and
# `Retrieval` below is what keeps them separate.
#
# Doing it in SQL rather than with a stopword list here is the point. `to_tsvector`
# already knows that "what", "did", "i", "about" and "the" carry nothing and that
# "landing" stems to `land` -- and it knows it with the *same* dictionary the index
# was built with, which a Python list could only approximate.
_LEXEMES = f"SELECT lexeme FROM unnest(to_tsvector('{CONFIG}', %(question)s))"

# The search. One statement, and the nesting is what makes it one.
#
# `hits` picks the rows and the order; the outer SELECT computes the headline. That
# split is not cosmetic -- a `ts_headline` in the inner target list would be
# evaluated for rows the LIMIT then throws away, which on this table means
# re-parsing every matching transcript to show three of them.
#
# `quote_literal` builds the tsquery, and it is what makes this safe with no
# escaping of our own: the lexemes are bound as a `text[]`, Postgres quotes each one
# into tsquery's own single-quoted form, and the operators between them are the two
# literal characters written here. Nothing a person typed reaches the parser as
# syntax.
#
# `ts_rank` rather than `ts_rank_cd`, and rather than the `created_at DESC` the
# list settled on. All three orderings were argued out in MemoRepository::list and
# the conclusion there does not carry: it rejected rank because rank cannot order
# the ILIKE arm and buries the in-flight pin, and neither exists here. What is left
# is the property rank has that recency does not -- a memo matching two words of the
# question outranks one matching one -- which is the entire job of a top-K
# prefilter. Recency is the tie-break, so two equally relevant memos still read
# newest first.
_SEARCH = f"""
    WITH q AS (
        SELECT to_tsquery('{CONFIG}', array_to_string(
                   ARRAY(SELECT quote_literal(term)
                           FROM unnest(%(lexemes)s::text[]) AS term), ' | '
               )) AS query
    ),
    hits AS (
        SELECT m.id,
               m.title,
               m.transcript,
               m.created_at,
               ts_rank(m.search_vector, q.query) AS rank
          FROM memos m, q
         -- The owner leads, and it is the only predicate here that is not about
         -- relevance. Without it this query retrieves across every memo in the
         -- database and the model answers one person's question out of another
         -- person's transcripts -- quoted verbatim in an excerpt, which is the
         -- worst shape that leak could take. It is first in the WHERE so that a
         -- reader checking whether Ask is scoped finds the answer immediately,
         -- and so the planner leads with the equality.
         WHERE m.owner_id = %(owner_id)s
           AND m.transcript IS NOT NULL
           AND btrim(m.transcript) <> ''
           AND m.search_vector @@ q.query
         ORDER BY rank DESC, m.created_at DESC
         LIMIT %(limit)s
    )
    SELECT hits.id,
           hits.title,
           hits.created_at,
           length(btrim(regexp_replace(hits.transcript, '\\s+', ' ', 'g'))) AS transcript_chars,
           ts_headline('{CONFIG}', hits.transcript, q.query, %(headline)s) AS excerpt
      FROM hits, q
     ORDER BY hits.rank DESC, hits.created_at DESC
"""

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Source:
    """
    One retrieved memo, as the model sees it and as the browser is told about it.

    ``ref`` is the load-bearing field and it is assigned here rather than produced
    by anything: it is the short label the prompt fences this memo with and the
    only thing the model is ever asked to cite. See memo_ai/ask/prompt.py for why a
    1.5B model is not shown a uuid.
    """

    ref: int
    id: UUID
    title: str | None
    created_at: datetime

    # The passage `ts_headline` chose, whitespace-collapsed and capped. Not the
    # transcript: nothing in this service sends a whole memo anywhere, and the
    # browser gets the same excerpt the model was shown so that a reader checking a
    # citation is checking what the answer was actually built from.
    excerpt: str

    # Whether the memo is longer than the excerpt. Carried so the UI can say "read
    # the memo" honestly rather than implying the excerpt is the whole of it, and so
    # a person debugging a wrong answer can see that the model was shown a fragment.
    #
    # A real comparison rather than a guess at one: the statement returns the
    # transcript's length *after* the same whitespace collapse `_excerpt` performs,
    # so a memo that only lost its line breaks is not reported as cut.
    truncated: bool


@dataclass(frozen=True)
class Retrieval:
    """
    What the prefilter found, with the two empty cases kept apart.

    ``sources`` empty and ``terms`` empty is "there was nothing in that question to
    look for"; ``sources`` empty and ``terms`` non-empty is "nothing you have said
    mentions that". Both skip the model entirely -- there is no answer to synthesise
    from no evidence, and twenty seconds spent producing a paragraph that says so
    would be twenty seconds worse than saying so immediately.
    """

    sources: tuple[Source, ...]
    terms: tuple[str, ...]

    @property
    def has_terms(self) -> bool:
        return bool(self.terms)


def retrieve(
    connection: psycopg.Connection,
    question: str,
    *,
    owner_id: str,
    top_k: int,
    memo_chars: int,
) -> Retrieval:
    """
    Find the memos this question is about, best match first.

    ``owner_id`` is keyword-only and has no default, deliberately. It is the one
    argument here that is not a tuning knob: a default would make an unscoped
    retrieval expressible, and the failure mode of an unscoped retrieval is that
    somebody's private transcript is quoted into somebody else's answer. Making it
    required means a caller who has not thought about whose memos these are cannot
    call this function at all.
    """
    terms = _terms(connection, question)

    if not terms:
        return Retrieval(sources=(), terms=())

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            _SEARCH,
            {
                "owner_id": owner_id,
                "lexemes": list(terms),
                "limit": top_k,
                "headline": HEADLINE_OPTIONS,
            },
        )
        rows = cursor.fetchall()

    sources = tuple(
        _source(ref, row, memo_chars) for ref, row in enumerate(rows, start=1)
    )

    log.info(
        "question matched %d memo(s) on %d term(s): %s",
        len(sources),
        len(terms),
        ", ".join(terms),
    )

    return Retrieval(sources=sources, terms=terms)


def _terms(connection: psycopg.Connection, question: str) -> tuple[str, ...]:
    with connection.cursor() as cursor:
        cursor.execute(_LEXEMES, {"question": question})

        return tuple(row[0] for row in cursor.fetchall())


def _source(ref: int, row: dict, memo_chars: int) -> Source:
    excerpt = _excerpt(row["excerpt"], memo_chars)

    return Source(
        ref=ref,
        id=row["id"],
        # Folded to None rather than passed on, so the prompt and the UI have one
        # spelling of "this memo has no title". A memo can reach `ready` with a
        # title cut from its transcript, but a row written by hand need not.
        title=_line(row["title"]),
        created_at=row["created_at"],
        excerpt=excerpt,
        # Against the whole transcript rather than against what ts_headline
        # returned, which is the honest comparison: a headline is a selection from
        # the memo whether or not this function then shortened it further.
        truncated=len(excerpt) < row["transcript_chars"],
    )


# What a cut excerpt ends in. Two characters, and they come out of the budget below
# rather than being added to it -- `ASK_MEMO_CHARS` is a prompt budget, and a cap
# that is quietly two over is a cap that stops being arithmetic.
_CUT = " …"


def _excerpt(text: object, limit: int) -> str:
    """
    One memo's evidence: whitespace collapsed, capped, cut on a word boundary.

    Collapsing is worth the loss of the speaker's line breaks. Every newline is a
    token, a transcript's paragraphing carries nothing a model needs, and a fenced
    block with blank lines in it is a block whose end marker is easier for a small
    model to lose track of.

    The result is never longer than ``limit``, including the marker, which is what
    lets memo_ai/ask/model.py size a context window from ``top_k * memo_chars``
    and be right.
    """
    if not isinstance(text, str):
        return ""

    collapsed = _WHITESPACE.sub(" ", text).strip()

    if len(collapsed) <= limit:
        return collapsed

    room = max(limit - len(_CUT), 0)

    # rpartition, and `_line` in memo_ai/enrich/local.py has the argument for why
    # not rsplit: on a run with no space in it, rsplit hands back the whole slice
    # and the cap is exceeded by the one input that has no word boundary to respect.
    head = collapsed[: room + 1].rpartition(" ")[0]

    return (head or collapsed[:room]).rstrip(" ,;:-") + _CUT


def _line(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    return _WHITESPACE.sub(" ", value).strip() or None

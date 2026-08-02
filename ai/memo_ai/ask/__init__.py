"""
Ask my memos (MEMO-24): retrieval over the existing full-text index, then a local
model that answers from what it found.

The package behind ``ai-api``, the sixth compose service -- the same image as
``ai-worker`` with ``python -m memo_ai.ask`` in place of ``python -m
memo_ai.worker``. Five modules, in the order a question moves through them:

  * :mod:`~memo_ai.ask.retrieval` -- which memos, decided by Postgres.
  * :mod:`~memo_ai.ask.prompt` -- the conversation, the fences around the evidence,
    and the citations that come back out.
  * :mod:`~memo_ai.ask.model` -- the resident llama.cpp model, streaming.
  * :mod:`~memo_ai.ask.service` -- the two of those in order, as events.
  * :mod:`~memo_ai.ask.app` -- FastAPI, turning events into NDJSON.

Nothing here writes to the database. Python owns ``transcript``, the queue columns
and the enrichment columns everywhere else in this package; on this path it is a
reader of ``memos`` and nothing more, which is the narrowest half of the ownership
split NOTES.md states.

Re-exported here so that ``from memo_ai.ask import Model`` keeps working if the
modules are ever rearranged -- the same courtesy ``memo_ai.enrich`` extends to the
pipeline and the test doubles.
"""

from memo_ai.ask.model import Model, ModelUnavailable, context_tokens
from memo_ai.ask.retrieval import Retrieval, Source, retrieve
from memo_ai.ask.service import Event, answer

__all__ = [
    "Event",
    "Model",
    "ModelUnavailable",
    "Retrieval",
    "Source",
    "answer",
    "context_tokens",
    "retrieve",
]

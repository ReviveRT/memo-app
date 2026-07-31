"""
The Python side of the stack: transcription and, from MEMO-21, enrichment.

One package, because one image serves both roles the compose file plans for.
``python -m memo_ai.worker`` is the queue consumer (MEMO-08) and ``ai-api``
(MEMO-24, a stretch) is a second entrypoint over the same code, which is what
"same image as ai-api, different entrypoint" means in practice: a ``command:``
override on a service, not a second build context.

Nothing in here imports the PHP side or is imported by it. The two runtimes meet
at exactly two places -- the ``memos`` table and the shared ``audio`` volume --
and both contracts are written down where they can be seen from both sides:
db/migrations/001_init.sql and NOTES.md.
"""

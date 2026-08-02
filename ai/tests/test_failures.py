"""
The failure *kind* that travels beside the sentence: the vocabulary, and how an
exception comes to carry one.

``last_error`` is prose and ``last_error_code`` is the token a program branches on --
db/migrations/004_last_error_code.sql has the argument, and the branch that matters is
the frontend's: a recording with nothing in it is deleted rather than shown, and every
other failure is kept with a Retry button.

This file covers the classification at rest. That the code then survives the trip to
the queue write is a pipeline question and is pinned in test_pipeline.py, next to the
fixtures that can run a job.

What is deliberately *not* pinned anywhere is the sentences. Those are worded where the
fault is detected and are expected to be reworded; the whole point of the code is that
nothing has to parse them, and asserting on both together would rebuild in the test
suite exactly the coupling the column removes.
"""

from memo_ai import audio, failures
from memo_ai.stt.base import SttError, SttUnavailable

# Every name in the vocabulary, so the two whole-set assertions below cannot quietly
# stop covering a code that was added after they were written.
ALL_CODES = (
    failures.NO_SPEECH,
    failures.NO_AUDIO,
    failures.TOO_LONG,
    failures.UNREADABLE,
    failures.PROVIDER_UNAVAILABLE,
    failures.TOO_SLOW,
    failures.INTERRUPTED,
    failures.ABANDONED,
    failures.TRANSCRIPTION_FAILED,
    failures.UNEXPECTED,
)


def test_an_error_carries_its_class_default_unless_the_raise_site_says_otherwise():
    # The class attribute is what a subclass declares once instead of every raise site
    # repeating it, and SttError only overwrites it when a code is actually passed.
    assert SttError("anything").code == failures.TRANSCRIPTION_FAILED
    assert SttUnavailable("anything").code == failures.PROVIDER_UNAVAILABLE
    assert audio.AudioError("anything").code == failures.UNREADABLE

    assert SttError("anything", code=failures.NO_SPEECH).code == failures.NO_SPEECH


def test_a_too_long_refusal_keeps_its_own_code_through_its_own_constructor():
    # AudioTooLong takes a second positional argument and calls up without a code, which
    # is exactly the shape that would break if SttError assigned `self.code = code`
    # unconditionally: the class attribute would be shadowed by None, and a memo refused
    # for its length would reach the row unclassified.
    refused = audio.AudioTooLong("Too long.", duration_ms=700_000)

    assert refused.code == failures.TOO_LONG
    assert refused.duration_ms == 700_000


def test_only_the_empty_recordings_are_discardable():
    # Pinned as a whole rather than one membership at a time, because the risk this set
    # carries is a code being *added* to it by someone reasoning about one failure in
    # isolation. Everything outside it is a real recording, and deleting one of those is
    # not recoverable -- the blob goes with the row.
    assert failures.DISCARDABLE == {failures.NO_SPEECH, failures.NO_AUDIO}

    kept = set(ALL_CODES) - failures.DISCARDABLE

    assert kept == {
        failures.TOO_LONG,
        failures.UNREADABLE,
        failures.PROVIDER_UNAVAILABLE,
        failures.TOO_SLOW,
        failures.INTERRUPTED,
        failures.ABANDONED,
        failures.TRANSCRIPTION_FAILED,
        failures.UNEXPECTED,
    }


def test_the_vocabulary_has_no_duplicate_values():
    # Two names sharing a value would make the frontend's discardable set match a kind it
    # was never meant to, which is the one way this design fails destructively. It is also
    # exactly what a copy-pasted constant produces, silently.
    assert len(set(ALL_CODES)) == len(ALL_CODES)


# There is deliberately no test here that web/src/memoFailure.js agrees with DISCARDABLE
# above, and it was written before being removed. The ai image bakes its own source at
# build time and mounts nothing (docker-compose.yml), so the documented way to run this
# suite -- `docker compose run --rm --no-deps ai-worker ... python -m pytest` -- has no
# `web/` to read. The test could only ever skip, and a test that always skips reads as
# coverage while providing none.
#
# So the two copies are kept in step the way this project already keeps MemoDialog's
# MAX_TITLE_LENGTH in step with UpdateMemoRequest's: each side names the other in a
# comment. It is a weaker guarantee and it is the one available across two runtimes that
# share no build step.


def test_every_statement_that_touches_one_error_column_touches_both():
    """
    The pairing rule, checked against the SQL rather than trusted.

    A row carrying a code with no sentence explains nothing to a person; one carrying a
    sentence with no code can be classified by nothing. So every write either sets both
    or clears both -- and this test exists because the rule was broken the first time it
    was applied: ``_REAP_SALVAGE`` was written before the code column and its single
    ``last_error = NULL`` looked complete, so a salvaged memo reached ``ready`` still
    carrying the code of the failure it recovered from.

    Reading the module's SQL constants is unusual and is the point: the alternative is
    six near-identical database tests, and what actually goes wrong here is a line
    missing from a statement, which is a thing the statement's own text can answer.
    """
    from memo_ai import memos

    statements = {
        name: value
        for name, value in vars(memos).items()
        if name.startswith("_") and isinstance(value, str) and "UPDATE memos" in value
    }

    # A guard on the guard: a rename that made this dict empty would pass silently.
    assert len(statements) >= 5, f"expected the UPDATE statements, found {sorted(statements)}"

    for name, sql in statements.items():
        # `last_error_code` contains `last_error`, so the sentence has to be counted by a
        # form that cannot match the code's own assignment.
        sets_sentence = "last_error =" in sql
        sets_code = "last_error_code =" in sql

        assert sets_sentence == sets_code, (
            f"{name} writes one of the error columns without the other"
        )

"""
What the shaping layer may and may not do to a transcript.

The file is organised around the one property that matters more than any single
formatting rule: ``test_the_words_are_never_touched`` runs over every input in this
module and asserts that shaping changed no word. A formatter that improved
somebody's wording would be a transcription error no test could tell from a model
failure, and that test is what lets the rest of these be liberal.

Nearly every fixture is verbatim from this project's own database or from
``tests/fixtures/``. They are the ones worth keeping: each formatting rule here was
written for something one of them actually did.
"""

import re

from memo_ai import prose

# A real 89-second memo as `large-v3-turbo` transcribed it before
# ``PUNCTUATION_PRIMER`` existed: 1204 characters, entirely lowercase, with not one
# comma in them. This is the transcript that caused this module and the primer both.
# Trimmed here to its first four segments.
#
# The one full stop in the whole memo is the one inside "index.html", which is not a
# sentence end and is why that case is tested above.
UNPUNCTUATED = (
    "database is running from yet another work free and nothing is running from "
    "main also that earlier 200 i trusted was misleading let me see what it "
    "actually returns and critically confirms your 38 memos are in a named volume "
    "before we create anything check the 200 body and whether database data is "
    "named volume both questions answered to that 200 was white spa fallbacks or "
    "index.html so"
)

# The same recording once the primer was in place, which is the transcript this
# project now produces: punctuated, capitalized, and still one unbroken block until
# this module gets hold of it.
PUNCTUATED = (
    "Database is running from yet another work-free and nothing is running from "
    "main. Also that earlier 200 I trusted was misleading. Let me see what it "
    "actually returns and critically confirms your 38 memos are in a named volume "
    "before you create anything. Check the 200 body and whether database data is "
    "named volume. Both questions answered to that 200 were white SPA fallbacks "
    "or index.html, so my earlier memo back drop view to 100 proves nothing. I "
    "used the status code where we should have checked the body. Postgres is on "
    "the named volume member app pg data, not a work-free bind."
)

# Every string this module shapes, for the invariant test at the bottom. Appended to
# by `shaped` rather than assembled by hand, so a test added later cannot forget to
# opt into it.
SHAPED: list[tuple[str, str]] = []


def shaped(text: str, language: str | None = "en") -> str:
    """Shape one string, and record it for ``test_the_words_are_never_touched``."""
    result = prose.shape(text, language)
    SHAPED.append((text, result))

    return result


# --------------------------------------------------------------------------
# spacing and punctuation hygiene
# --------------------------------------------------------------------------


def test_stray_space_before_punctuation_closes_up():
    assert shaped("  hello   world , this  is  fine .  ") == "Hello world, this is fine."


def test_newlines_from_a_provider_are_not_kept():
    # Line breaks in the output are this module's to place. A newline arriving in
    # the input reflects whoever chunked it, not the speech.
    assert shaped("one thing.\n\nanother thing.") == "One thing. Another thing."


def test_a_missing_space_after_a_comma_is_added_but_not_inside_a_number():
    # The number half is the one worth pinning. A rule that "fixed" 1,000 into
    # 1, 000 would corrupt the part of a memo likeliest to be a fact somebody
    # needs, and 12:30 is the same trap with a colon.
    assert shaped("hello,world and 1,000 more at 12:30") == "Hello, world and 1,000 more at 12:30."


def test_a_missing_space_after_a_full_stop_is_added():
    assert shaped("the body.Postgres is on the volume") == "The body. Postgres is on the volume."


def test_doubled_dots_collapse_and_an_ellipsis_survives():
    assert shaped("Wait.. really? Yes....") == "Wait. Really? Yes..."


def test_a_spaced_dot_run_is_closed_up_before_it_is_read():
    # Ordering inside _hygiene, and it was wrong: the run normalizer used to look
    # before the spaces were closed, so "a . . b" reached sentence splitting as
    # "a.. b" -- a run the terminator rules then declined to read as anything.
    assert shaped("Yes . . . maybe.") == "Yes... maybe."
    assert shaped("spaced .  . dots") == "Spaced. Dots."


def test_a_comma_stranded_at_the_start_of_a_sentence_is_dropped():
    # How whisper joins segments: it finishes a sentence at the end of one and
    # opens the next with a continuation, so the marks meet with the weaker second.
    assert shaped("That is the note., and then more.") == "That is the note. And then more."


# --------------------------------------------------------------------------
# what is and is not the end of a sentence
# --------------------------------------------------------------------------


def test_an_abbreviation_does_not_end_a_sentence():
    # "12 p.m." is verbatim from a real memo here, and what the single-letter guard
    # was written for.
    assert shaped("I need this at 12 p.m. on Sunday. Dr. Smith agreed.") == (
        "I need this at 12 p.m. on Sunday. Dr. Smith agreed."
    )


def test_a_comma_after_an_abbreviation_is_kept():
    # The counterpart to test_a_comma_stranded_at_the_start_of_a_sentence: here the
    # full stop belongs to the abbreviation, so the comma is correct and a blanket
    # "drop a comma after a full stop" rule would eat it.
    assert shaped("I need it at 12 p.m., and then we go.") == "I need it at 12 p.m., and then we go."


def test_a_filename_is_not_two_sentences():
    # "index.html" is verbatim from the real transcript, and it is what the
    # whitespace test in _ends_sentence exists for. Without it this read as a
    # boundary and produced "index. Html today."
    assert shaped("check index.html today and example.com too") == (
        "Check index.html today and example.com too."
    )


def test_a_decimal_point_and_a_numbered_reference_do_not_end_a_sentence():
    assert shaped("It cost 1.5 million and took no. 5 place.") == (
        "It cost 1.5 million and took no. 5 place."
    )


def test_an_ellipsis_does_not_start_a_new_sentence():
    # Hesitation, not a boundary. Cutting here would capitalize "maybe" and turn one
    # trailing-off sentence into two half ones.
    assert shaped("I was thinking... maybe not.") == "I was thinking... maybe not."


def test_a_word_welded_to_a_terminator_is_not_capitalized():
    """
    The same rule, applied where the two halves of this module could contradict.

    A sentence can begin with terminators when the one before it ended on a different
    mark -- "Wait!" then "...then he left". Capitalizing there produces "...Then",
    which is precisely the pattern the missing-space rule inserts a space into, so
    shaping twice gave "... Then" and idempotence broke. Found by fuzzing, not by
    reasoning: 126 inputs out of 12,624 hit it.
    """
    assert shaped("Wait! ...then he left.") == "Wait! ...then he left."
    assert shaped("...maybe not.") == "...maybe not."

    # An opening quote or bracket is not a terminator, so those still capitalize.
    assert shaped("'go now.' he said.") == "'Go now.' He said."
    assert shaped("(one thing.) two things.") == "(One thing.) Two things."


def test_a_sentence_ending_on_a_number_is_not_terminated_twice():
    # The digit-before-the-dot case. An earlier version read "12:30." as a decimal,
    # failed to see a finished sentence, and appended a second full stop.
    assert shaped("It was 1,000 dollars at 12:30.") == "It was 1,000 dollars at 12:30."


def test_a_closing_quote_stays_with_the_sentence_it_closes():
    assert shaped("he said 'go.' then left") == "He said 'go.' Then left."


def test_an_unterminated_transcript_gets_a_full_stop():
    # Verbatim from the database: three seconds of accented English, correct to the
    # word and with nothing on the end of it.
    assert shaped("I would like to place an order") == "I would like to place an order."


def test_a_transcript_ending_on_a_comma_is_closed_rather_than_stacked():
    assert shaped("so i went to the shop and then,") == "So I went to the shop and then."


# --------------------------------------------------------------------------
# capitals
# --------------------------------------------------------------------------


def test_the_first_letter_of_every_sentence_is_capitalized():
    assert shaped("this is one. this is two. is this three?") == (
        "This is one. This is two. Is this three?"
    )


def test_the_english_pronoun_is_capitalized():
    assert shaped("i think i will. i'm sure and i'll go.") == "I think I will. I'm sure and I'll go."


def test_the_pronoun_rule_is_english_only():
    # `i` is "and" in Polish. Capitalizing it there is not a fix, it is a typo
    # introduced into every sentence containing one -- and the sentence-initial
    # capital still applies, which is what the second line checks.
    assert shaped("kot i pies siedzą razem", "pl") == "Kot i pies siedzą razem."
    assert shaped("i pies, i kot", "pl") == "I pies, i kot."


def test_an_unknown_language_declines_the_pronoun_rule():
    assert shaped("kot i pies", None) == "Kot i pies."


def test_a_sentence_opening_on_a_number_is_left_alone():
    # An earlier version reached past the digits for the first letter it could find
    # and produced "12 P.m. is fine."
    #
    # The third sentence is load-bearing, and this test was wrong without it. With
    # only the first two, both readings -- one sentence or two -- print the same
    # string, because a sentence opening on a digit has no capital to gain. A
    # lowercase word after the second full stop is what makes a missed boundary
    # visible.
    assert shaped("12 p.m. is fine. 38 memos survived. it worked.") == (
        "12 p.m. is fine. 38 memos survived. It worked."
    )


def test_a_full_stop_before_a_number_still_ends_a_sentence():
    # The bug this replaced: the numbered-reference rule tested only for a digit
    # after the dot, so *any* word followed by one stopped ending a sentence. "That
    # is done. 38 memos survived" was a single sentence, which cost the capital on
    # the next word wherever there was one, and made the paragraph rule undercount.
    assert shaped("that is done. 38 memos survived. it worked.") == (
        "That is done. 38 memos survived. It worked."
    )

    # And the label case it was protecting is still protected -- both halves needed,
    # the label *and* the numeral.
    assert shaped("I need no. 5 please. see fig. 2 as well.") == (
        "I need no. 5 please. See fig. 2 as well."
    )


def test_a_contraction_before_a_full_stop_still_ends_a_sentence():
    """
    The widest-reaching bug this module had.

    The lookbehind for the abbreviation tests matched letters only, so "don't." ends
    in the single letter ``t`` -- which the initial test read as the ``p`` of "p.m."
    and refused to end the sentence on. Every ``n't`` contraction in English was
    affected, and they are among the commonest words in dictated speech, so "I don't.
    It's fine." came out as one sentence with a lowercase "it's" inside it.
    """
    assert shaped("I don't. it's fine.") == "I don't. It's fine."
    assert shaped("he didn't. she couldn't. they wouldn't.") == (
        "He didn't. She couldn't. They wouldn't."
    )

    # The typographic apostrophe too: whisper emits the straight one, but a hosted
    # provider behind this same shaping may not.
    assert shaped("I don’t. it’s fine.") == "I don’t. It’s fine."

    # And a word that only *contains* an apostrophe is still an ordinary word.
    assert shaped("it's 5 o'clock. we should go.") == "It's 5 o'clock. We should go."


def test_an_ordinary_word_is_not_treated_as_an_abbreviation():
    """
    Every entry in the abbreviation list is a word that can never end a sentence,
    so ordinary words must not be in it.

    They were. An earlier list carried the weekdays and months, and `sat`, `sun`,
    `wed` and `mar` are all ordinary English words -- so "he sat. then he stood"
    came out as one sentence with a lowercase "then" in the middle of it. A
    suppressed boundary suppresses the capital after it too, which is what made this
    visible rather than merely wrong.
    """
    assert shaped("he sat. then he stood.") == "He sat. Then he stood."
    assert shaped("the sun. it was bright.") == "The sun. It was bright."
    assert shaped("they wed. then they left.") == "They wed. Then they left."
    assert shaped("i ate a fig. then i left.") == "I ate a fig. Then I left."

    # The titles whisper really does emit are still abbreviations.
    assert shaped("Mr. Smith and Dr. Jones went. then they left.") == (
        "Mr. Smith and Dr. Jones went. Then they left."
    )


def test_a_camel_cased_word_keeps_its_own_spelling():
    assert shaped("iPhone is fine. macOS too.") == "iPhone is fine. macOS too."


def test_nothing_is_ever_lowercased():
    # A capital mid-sentence is far likelier to be a name than a mistake. "SPA" and
    # "index.html" are both from the real transcript.
    assert shaped("both answers were white SPA fallbacks or index.html today") == (
        "Both answers were white SPA fallbacks or index.html today."
    )


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_a_short_memo_stays_one_paragraph():
    # Verbatim from the database, and the common case: most memos are a sentence or
    # two and have nothing to break up.
    assert "\n" not in shaped(
        "Please remind me to call my lawyer tomorrow, and here are three ideas for "
        "my landing page, so it should be the nice game visit card."
    )


def test_a_transcript_at_the_cap_is_not_broken_up():
    five = " ".join(f"This is sentence {n}." for n in range(1, 6))

    assert "\n" not in shaped(five)


def test_paragraphs_are_divided_evenly_rather_than_filled_to_the_cap():
    # Six sentences greedily filled would be a paragraph of five and an orphan of
    # one. Divided, they are two of three.
    six = " ".join(f"This is sentence {n}." for n in range(1, 7))
    paragraphs = shaped(six).split("\n\n")

    assert [len(p.split(".")) - 1 for p in paragraphs] == [3, 3]


def test_a_long_transcript_is_broken_into_paragraphs_of_whole_sentences():
    paragraphs = shaped(PUNCTUATED).split("\n\n")

    assert len(paragraphs) > 1

    for paragraph in paragraphs:
        assert paragraph.strip() == paragraph
        assert paragraph.endswith((".", "!", "?")), paragraph
        assert len(re.findall(r"[.!?](?:\s|$)", paragraph)) <= prose.PARAGRAPH_MAX_SENTENCES


def test_the_unpunctuated_transcript_gets_what_shaping_can_honestly_give_it():
    """
    The regression this module was written alongside.

    The input is real: one lowercase run whose only full stop is inside a filename.
    Nothing here can recover the sentences the speaker actually said -- that is the primer's job
    in memo_ai/stt/local.py, and the module docstring says why it cannot be done
    from the words. So what is asserted is what shaping alone honestly delivers: a
    capital at the front, a full stop at the end, the pronoun fixed, and the words
    untouched.
    """
    out = shaped(UNPUNCTUATED)

    assert out.startswith("Database is running")
    assert out.endswith(".")
    assert " i " not in out

    # And it is still one paragraph, because there is exactly one sentence in it to
    # divide. Overstating the structure would be worse than leaving it alone.
    assert "\n" not in out


def test_empty_and_blank_input_produce_nothing():
    # The local provider turns "" into its own "no speech" error rather than writing
    # a blank memo, so these have to be "" and not " " or ".".
    assert prose.shape("") == ""
    assert prose.shape("   \n  ") == ""


def test_input_with_no_words_in_it_produces_nothing():
    # Reachable, not hypothetical: these survive hygiene, and the rules below would
    # dutifully capitalize and terminate them into "..." and ".". A memo containing
    # one punctuation mark is worse than the provider's "no speech" error.
    assert prose.shape("...") == ""
    assert prose.shape(",") == ""
    assert prose.shape(" ?! ") == ""

    # But a memo that is only the number somebody said is words enough.
    assert prose.shape("15") == "15."


def test_shaping_is_idempotent():
    """
    Shaping already-shaped text changes nothing.

    Worth pinning because the worker can shape the same recording twice -- MEMO-16's
    retry re-runs a job whose transcript is already written -- and a rule that
    appended a full stop or a paragraph break on every pass would drift.
    """
    for source in (PUNCTUATED, UNPUNCTUATED, "I would like to place an order"):
        once = prose.shape(source, "en")

        assert prose.shape(once, "en") == once


# --------------------------------------------------------------------------
# the invariant
# --------------------------------------------------------------------------


def test_the_words_are_never_touched():
    """
    Over every input this module shaped: the same words, in the same order.

    Case is excluded because changing it is half of what this module is for, and
    punctuation because the other half is inserting it. What is left is the claim
    worth making -- that no word was added, dropped, reordered or respelled -- in
    the one column of this application that is a record of what somebody said.
    """
    assert SHAPED, "the fixtures above did not run"

    for source, result in SHAPED:
        assert _words(source) == _words(result), source


def _words(text: str) -> list[str]:
    """
    Lowercased word tokens, so case and punctuation drop out of a comparison.

    Apostrophes are kept inside a word so that "I'll" stays one token: without that,
    the contraction whisper produces would compare equal to "i" plus "ll" and this
    would pass a formatter that had split it.
    """
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.lower())


def test_the_word_comparison_would_notice_a_changed_word():
    # The invariant test is worth only as much as this helper, so the helper gets its
    # own check: a formatter that dropped, added or respelled a word must not slip
    # past it.
    assert _words("i would like to place an order") != _words("i would like to blaze an order")
    assert _words("call the lawyer") != _words("call the lawyer tomorrow")
    assert _words("I'll go") != _words("I will go")

    # And what it is supposed to ignore, it ignores.
    assert _words("hello world") == _words("Hello, world!")

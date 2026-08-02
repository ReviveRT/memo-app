"""
What memo_ai.titles makes of a transcript.

Every case here is a whole transcript in, a whole title out, because that is the only
contract this module has -- the internals are three regexes and a word list, and tests
against those would pin the implementation rather than the behaviour.

The invariant test at the bottom is the one worth keeping if the rest ever have to be
rewritten: this module may drop words and may replace a list with a fixed phrase, and
it may do nothing else to what somebody said.
"""

import pytest

from memo_ai.titles import MAX_TITLE_CHARS, MAX_TITLE_WORDS, title_for


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        # The opener a spoken memo actually starts with. Eight words of somebody
        # working out that they have started recording, then the memo.
        (
            "Hello, I would like to leave a note to book tomorrow tickets on the "
            "airplane to Moscow.",
            "Book tomorrow tickets",
        ),
        # Fillers stack, so they are stripped in a loop rather than once.
        ("Okay, so, note to self, ring the bank.", "Ring the bank"),
        ("note to self: ring the bank about the mortgage", "Ring the bank"),
        # "about" ends the part of the sentence that names the memo.
        (
            "Remember to call the dentist about the appointment on Thursday morning.",
            "Call the dentist",
        ),
        # A comma is where the speaker finished saying what they meant.
        (
            "So the thing is, the deploy failed again because the migration timed out.",
            "The deploy failed again",
        ),
        # A memo that opens with its own heading keeps it, which is the best case.
        ("Sunday Meeting. We discussed the budget and then hiring.", "Sunday Meeting"),
        # Short enough to be its own title. (The trailing-date case for this same
        # sentence lives with the other date rules below.)
        ("Sort the invoices.", "Sort the invoices"),
    ],
)
def test_the_title_is_the_first_thought_with_the_throat_clearing_removed(transcript, expected):
    assert title_for(transcript) == expected


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        # The reported bug, verbatim. Before the date and the light-verb opener were
        # stripped this came out "Tomorrow I will have" -- four function words, no
        # subject, and a title that says nothing about the memo.
        (
            "Tomorrow I will have a meeting with my friend John at 15am.",
            "Meeting with my friend John",
        ),
        (
            "Tomorrow I have a call with Sarah from the bank at 3pm.",
            "Call with Sarah from the bank",
        ),
        (
            "On Friday we have a review of the quarterly numbers at 14:30.",
            "Review of the quarterly numbers",
        ),
        ("Next week I need to renew the passport before the trip.", "Renew the passport before the trip"),
        ("There is a problem with the boiler in the kitchen.", "Problem with the boiler"),
        # The trailing date goes even without a leading one.
        ("Sort the invoices before Friday.", "Sort the invoices"),
        ("Send the report to the finance team by Friday.", "Send the report"),
    ],
)
def test_the_date_and_the_words_in_front_of_the_subject_are_dropped(transcript, expected):
    assert title_for(transcript) == expected


def test_a_bare_weekday_is_a_name_rather_than_a_date():
    # "Sunday Meeting" is the best case this module has: the speaker said what the thing
    # is called. Only a weekday introduced by on/this/next is being used as a date.
    assert title_for("Sunday Meeting. We discussed the budget.") == "Sunday Meeting"


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Today's numbers are in and they look good.", "Today's numbers are"),
        ("Tomorrow's delivery needs signing for.", "Tomorrow's delivery needs signing"),
        ("This week's report is late.", "This week's report is late"),
    ],
)
def test_a_possessive_date_is_a_word_rather_than_a_date(transcript, expected):
    # `\b` sits between a word character and a non-word one, and an apostrophe is not a
    # word character -- so the date pattern matched "Today" inside "Today's" and left
    # the title as "'s numbers are". Every possessive form of every word in the pattern
    # had it. Found by running the rule over ordinary sentences rather than examples
    # written to exercise it.
    assert title_for(transcript) == expected


def test_a_date_is_not_stripped_down_to_one_word():
    # "Launching" is shorter than the sentence it came from and says less. The date rule
    # earns its place by clearing room for a subject the cap would otherwise eat, not by
    # removing most of a short memo.
    assert title_for("We're launching on Tuesday.") == "Launching on Tuesday"


def test_a_memo_that_is_only_a_date_keeps_it():
    # The floor on the leading-date rule. A date is content -- here it is the whole of
    # the content -- so stripping it as an "opener" would leave the memo unnamed.
    assert title_for("Tomorrow morning.") == "Tomorrow morning"


def test_a_number_that_is_not_a_clock_survives():
    # `at 30` is a price, not a time. The clock pattern requires am/pm or a colon for
    # exactly this reason; without it the end of this memo is its whole content.
    assert title_for("The share price closed at 30 today.") == "The share price closed at 30"


def test_a_longer_filler_wins_over_a_shorter_one_that_prefixes_it():
    # "i have to" must be tried before "i have", or this loses three words instead of
    # five. The list is sorted longest-first at import so source order cannot break it.
    assert title_for("I have to call the bank about the mortgage.") == "Call the bank"


def test_a_list_of_things_to_buy_is_named_rather_than_quoted():
    # Three items after an acquisition verb. The first four items would be a worse
    # title than the two words that say what the memo is.
    assert (
        title_for("Buy milk, eggs, bread, coffee and a bag of rice on the way home.")
        == "Shopping list"
    )
    assert title_for("Order paper, toner, envelopes and a new stapler.") == "Shopping list"


def test_two_items_are_a_sentence_rather_than_a_list():
    # Naming this "Shopping list" would throw away the whole content of the memo to
    # gain nothing: it already fits in a title.
    assert title_for("Buy milk and bread.") == "Buy milk and bread"


def test_a_list_of_documents_is_not_called_a_shopping_list():
    # The measurement that cut `get`, `grab` and `pick` out of the vocabulary. A rule
    # that *names* a memo can assert something false, which is a different kind of
    # wrong from a rule that only shortens -- so the vocabulary is three unambiguous
    # verbs and everything else falls through to the generic path.
    assert (
        title_for(
            "Pick up the passport, the birth certificate, the marriage certificate "
            "and the deeds."
        )
        == "Pick up the passport"
    )


def test_a_filler_is_only_stripped_as_a_whole_word():
    # Without the word-boundary check, "so" eats the front of "Sort" and "hi" the
    # front of "Hire".
    assert title_for("Sort the mail.") == "Sort the mail"
    assert title_for("Hire a plumber for the leak upstairs.") == "Hire a plumber"


def test_a_title_never_ends_on_a_dangling_word():
    # Six words lands on "the"; the dangler is dropped rather than left hanging.
    assert title_for("Send the report to the finance team by Friday.") == "Send the report"


@pytest.mark.parametrize("transcript", [None, "", "   ", "...", "Um, okay, so..."])
def test_nothing_to_name_is_none_rather_than_a_placeholder(transcript):
    # None, so the column stays NULL and each reader applies its own fallback -- the
    # API coalesces to the transcript in SQL and the browser's memoLabel does the same
    # in JavaScript. A placeholder written here would defeat both.
    assert title_for(transcript) is None


def test_the_caps_are_honoured_in_words_and_in_characters():
    long_sentence = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet."
    title = title_for(long_sentence)

    assert title is not None
    assert len(title.split()) <= MAX_TITLE_WORDS
    assert len(title) <= MAX_TITLE_CHARS

    # One very long word always survives, because an empty title is worse than an
    # over-long one.
    single = "Supercalifragilisticexpialidociousandthensomemoreletters" * 2
    assert title_for(single) == single[0].upper() + single[1:]


@pytest.mark.parametrize(
    "transcript",
    [
        "Hello, I would like to leave a note to book tickets to Moscow.",
        "Remember to call the dentist about the appointment on Thursday.",
        "The quarterly numbers came in above forecast this time.",
        "Sort the invoices before Friday.",
        "iPhone repair booked for Tuesday afternoon at the shop.",
    ],
)
def test_the_words_are_never_touched(transcript):
    """
    The invariant, borrowed from memo_ai.prose and enforced the same way.

    Every word in a title is a word that was said, in the order it was said, with only
    its first letter allowed to change case. The one exception is the list rule, which
    replaces the title wholesale rather than editing it -- so these cases are chosen to
    avoid it.

    A titler that quietly improved somebody's wording would be indistinguishable from a
    transcription error.
    """
    title = title_for(transcript)

    assert title is not None

    words = title.split()
    spoken = transcript.split()

    # Same words, same order, from the start of some suffix of the transcript.
    start = spoken.index(words[0]) if words[0] in spoken else -1

    if start == -1:
        # The first word had its case changed by the capital rule; match on the rest.
        lowered = [word.lower() for word in spoken]
        start = lowered.index(words[0].lower())

    for at, word in enumerate(words):
        assert word.lower().strip(".,;:!?") == spoken[start + at].lower().strip(".,;:!?")


def test_iphone_keeps_its_own_spelling():
    # The capital rule only touches a lowercase first letter, so a word that owns its
    # capitalisation is left alone -- the same rule prose.py's _capitalize applies.
    assert title_for("iPhone repair booked for Tuesday.") == "iPhone repair booked for Tuesday"

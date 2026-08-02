"""
A short name for a memo, cut from its own transcript.

**What this is not.** It is not a summariser, and it cannot become one. Whisper is a
speech-to-text model with no summarisation head, so there is no "one more layer" to
add to the transcription pass -- the natural place to ask for a title is a language
model, which is what ``ENRICH_MODEL`` and ``ANTHROPIC_API_KEY`` in ``.env.example``
exist for. This module is the answer for a stack running with neither: no key, no
credits, no second model download, no network.

**What it does instead.** Four rules, in order, each of which only ever *removes*
words:

  1. Drop a leading run of filler and of date -- the throat-clearing that starts a
     spoken memo, and the four function words that usually follow it. "Tomorrow I will
     have a meeting with my friend John" is a memo about a meeting; the first five
     words are a date and a way of saying that something is going to happen.
  2. Cut at the first clause boundary, so a title is one thought rather than the
     opening of a sentence that carries on.
  3. Drop a time expression at the end, which is the least useful part of a title --
     the card already shows the date, and reminders are their own feature with their
     own field.
  4. Cap what is left at a few words, ending on a word boundary.

Plus one rule that *names* rather than shortens: a memo whose first verb is one of a
small set of gathering verbs, followed by several comma-separated items, is a list,
and "Shopping list" is a better title than the first four things on it. That is the
one place here that infers anything, and its vocabulary is deliberately tiny -- see
``_LIST_VERBS``.

**The invariant, borrowed from memo_ai/prose.py and for the same reason.** Nothing
here adds, reorders or respells a word: every rule drops words from the front or the
back, and the one exception (the list rule) replaces the whole title with a fixed
phrase rather than editing what was said. A titler that quietly improved somebody's
wording would be indistinguishable from a transcription error, in an app whose one
job is to be a record of what was said.

**How good it is, and where it stops.** Honest examples, all of them run through
``title_for`` in the test suite:

    "Tomorrow I will have a meeting with my friend John at 15am."
                                                 -> "Meeting with my friend John"
    "Hello, I would like to leave a note to book tomorrow tickets on the
     airplane to Moscow."                        -> "Book tomorrow tickets"
    "Buy milk, eggs, bread, coffee and rice."    -> "Shopping list"
    "Remember to call the dentist about the appointment on Thursday morning."
                                                 -> "Call the dentist"
    "So the thing is, the deploy failed again because the migration timed out."
                                                 -> "The deploy failed again"
    "On Friday we have a review of the quarterly numbers at 14:30."
                                                 -> "Review of the quarterly numbers"

And where it does not reach: it cannot look at a list of eighteen document names and
answer "Documents for the job", because that requires knowing what the items *are*.
It will answer with the first few of them. A model can do that and this cannot, which
is the trade for costing nothing; the title is editable in the UI precisely because a
guess this cheap is sometimes going to be wrong.
"""

import re

# How long a title may be, in words and in characters.
#
# Words first, because a title is read as a phrase and six of them is where a phrase
# starts becoming a sentence. The character cap is the backstop for six very long
# words, and it is well under the API's 200-character validation limit -- that number
# bounds what a *person* may type, and this one is what the machine allows itself.
MAX_TITLE_WORDS = 6
MAX_TITLE_CHARS = 60

# The throat-clearing a spoken memo starts with.
#
# Applied repeatedly, so "Hello, so, I would like to remember to call..." loses all
# four openers rather than one. Every entry is a phrase that carries no information
# about what the memo is *about* -- which is the test for belonging here, and the
# reason "note to self" is in the list while "note that" is not.
#
# Matched against a lowercased copy and stripped from the original, so the words that
# survive keep whatever case they were spoken with.
#
# **Sorted longest-first at import, not by hand.** The matcher takes the first entry
# that fits, so "i have to" must be tried before "i have" or "I have to call the bank"
# loses three words instead of five and comes out as "To call the bank". Relying on
# source order for that is a trap for the next person to add a phrase in the obvious
# place; sorting removes the hazard rather than documenting it.
_FILLER_PHRASES = (
    # Openers with no content at all.
    "hello there",
    "hello",
    "hi there",
    "hi",
    "hey",
    "okay so",
    "okay",
    "ok",
    "so basically",
    "so yeah",
    "so",
    "well",
    "um",
    "uh",
    "erm",
    "right",
    "anyway",
    "please",
    "the thing is",
    # Announcing that a memo is a memo.
    "just a quick note",
    "quick note",
    "note to self",
    "a note to self",
    "this is a note about",
    "this is a note",
    "i would like to leave a note to",
    "i would like to leave a note",
    # Intention, which is the frame rather than the thing.
    "i would like to",
    "i want to",
    "i need to",
    "i have to",
    "i should",
    "i must",
    "i just wanted to",
    "i wanted to",
    "let me",
    "let us",
    "let's",
    "remember to",
    "remember that",
    "don't forget to",
    "do not forget to",
    "make sure to",
    "make sure you",
    #
    # **Subject, auxiliary, light verb, article** -- the four words in front of the
    # noun that a spoken memo almost always has, and the group that made the
    # difference. "Tomorrow I will have a meeting with my friend John at 15am" is a
    # memo about a meeting, and without these the title came out "Tomorrow I will
    # have": four function words, no subject, and the reported bug.
    #
    # "have", "got" and "get" only. Not "make", "do" or "take", which carry meaning
    # of their own -- "I will make a cake" is about making, and "A cake" would lose
    # the half that matters.
    "i am going to",
    "i'm going to",
    "we are going to",
    "we're going to",
    "i will have an",
    "i will have a",
    "i will have",
    "i'll have an",
    "i'll have a",
    "i'll have",
    "i have got an",
    "i have got a",
    "i've got an",
    "i've got a",
    "i have an",
    "i have a",
    "we will have an",
    "we will have a",
    "we will have",
    "we'll have an",
    "we'll have a",
    "we have an",
    "we have a",
    "there is an",
    "there is a",
    "there's an",
    "there's a",
    "there are",
    "i am",
    "i'm",
    "we are",
    "we're",
)

_FILLERS = tuple(sorted(_FILLER_PHRASES, key=len, reverse=True))

_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_DAYPARTS = "morning|afternoon|evening|night"
_PERIODS = f"{_DAYPARTS}|week|weekend|month|year|{_WEEKDAYS}"

# A time expression the memo opens with.
#
# A regex rather than more entries in the list above, because the shape is generative:
# "next Tuesday", "this evening", "on Friday", "at 3pm" are one pattern each and forty
# strings written out. It is applied in the same loop as the fillers, so a memo that
# opens "Tomorrow morning, I need to..." loses both.
#
# **A bare weekday is deliberately absent.** "Sunday Meeting" is a real title and one
# of the best this module produces -- the speaker said the name of the thing. Only a
# weekday introduced by "on", "this" or "next" is being *used* as a date, and only
# those are stripped.
#
# **The lookahead is `(?![\w'’])` and not `\b`, which is the fix for a real bug.** `\b`
# sits between a word character and a non-word one, and an apostrophe is not a word
# character -- so "Today's numbers are in" matched "Today", left "'s numbers are in",
# and titled itself "'s numbers are". Every possessive form of every word in this
# pattern had it: today's, tomorrow's, this week's. Excluding the apostrophe as well as
# the letter is what makes the match a whole word rather than a prefix of one.
_LEADING_TIME = re.compile(
    rf"""^(?:
        (?:tomorrow|today|tonight|yesterday|later|soon)(?:\s+(?:{_DAYPARTS}))?
      | (?:this|next|last)\s+(?:{_PERIODS})
      | on\s+(?:{_WEEKDAYS})(?:\s+(?:{_DAYPARTS}))?
      | in\s+the\s+(?:{_DAYPARTS})
    )(?![\w'’])""",
    re.IGNORECASE | re.VERBOSE,
)

# The same thing at the other end, which is where it does most of the damage.
#
# "Meeting with my friend John at 15am" is seven words, so the six-word cap used to eat
# the name to make room for the clock. A time at the end of a memo is the least useful
# part of a *title* -- the card shows the date, and a reminder is a separate feature
# with its own field -- so it goes before the cap is applied rather than competing with
# the subject for room.
#
# The clock pattern requires am/pm or a colon. `at\s+\d+` alone would strip the end off
# "the share price closed at 30", which is not a time and is the whole content of that
# memo.
#
# `(?<![\w'’])` after the whitespace, so the match starts at a word boundary rather than
# inside one. Without it a memo ending in a word that happens to finish with one of these
# -- and the day names are the ones to worry about -- would lose its last syllables.
_TRAILING_TIME = re.compile(
    rf"""[\s,]*(?<![\w'’])(?:
        at\s+\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|a\.m\.|p\.m\.|o'clock)
      | at\s+\d{{1,2}}:\d{{2}}
      | (?:on|by|before|after)\s+(?:{_WEEKDAYS})(?:\s+(?:{_DAYPARTS}))?
      | (?:this|next|last)\s+(?:{_PERIODS})
      | (?:tomorrow|today|tonight|yesterday)(?:\s+(?:{_DAYPARTS}))?
      | in\s+the\s+(?:{_DAYPARTS})
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# The fewest words a trailing-date strip may leave behind.
#
# "We're launching on Tuesday" is three words after the filler goes, and taking the date
# off it leaves "Launching" -- which is shorter and says less than the sentence it came
# from. The rule is worth having when it clears room for a subject the cap would
# otherwise have eaten, and worth declining when the date *is* most of what was said.
MIN_WORDS_AFTER_DATE = 2

# Punctuation and connectives that end the part of a sentence worth putting in a
# title.
#
# A title should be the first thought, not the first sentence: "Call the dentist
# about the appointment on Thursday morning" is one thought too many, and the "about"
# is where it stopped being a name. These are the markers that reliably introduce the
# elaboration rather than the subject.
#
# `,` and `;` are here and `'s` is not, which is the distinction: a comma in speech is
# where the speaker took a breath after saying what they meant, while an apostrophe is
# inside a word.
_CLAUSE_BREAK = re.compile(
    r"""
    \s*[,;:]\s*                 # a breath
  | \s+(?:because|since|so\s+that|so|but|and\s+then|then|about|regarding|
         which|who|that\s+is|,)\s+
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Verbs that, in front of a list, mean the memo *is* a shopping list.
#
# **Three words, and it was six.** The cut is the whole lesson of this rule. `get`,
# `grab` and `pick` (as in "pick up") all start perfectly ordinary shopping memos, and
# all three also start memos that are lists of something else entirely: "Pick up the
# passport, the birth certificate, the marriage certificate and the deeds" is a list of
# *documents*, and this rule confidently called it "Shopping list" until they were
# removed. Measured on that exact sentence.
#
# That is the failure mode a naming rule has and a shortening rule does not: dropping
# words can only lose information, while replacing them can assert something false. So
# what is left is the three verbs that mean *acquire* and nothing else. Everything
# removed still gets a title -- it falls through to the generic path and comes back as
# "Pick up the passport", which is worse than a model would do and is at least about
# the right subject.
_LIST_VERBS = frozenset("buy purchase order".split())

# How many items make a list rather than a sentence with a comma in it.
#
# Three, counting the trailing "and X" as one. Two is "buy milk and bread", which is
# short enough to be its own title and better as one -- naming it "Shopping list"
# would be throwing away information to gain nothing.
MIN_LIST_ITEMS = 3

_LIST_SPLIT = re.compile(r",|\band\b", re.IGNORECASE)

_WORD = re.compile(r"\w")

# Words a title must not end on.
#
# The word cap cuts at a fixed count, which lands mid-phrase often enough to matter:
# "Send the report to the finance team" capped at six words is "Send the report to the",
# and a title ending in "the" reads as a bug rather than as a name. Dropping the dangler
# gives "Send the report", which is shorter and is a title.
#
# Only words that cannot end an English phrase -- articles, conjunctions and the
# commonest prepositions. Deliberately not every preposition: "came in above" is odd but
# it is a phrase, and trimming it back to "came" would be this rule inventing a shorter
# sentence rather than removing a loose end.
_DANGLING = frozenset(
    "a an the and or but of to for with in on at by from into about as that than is was".split()
)

# A word that is lowercase first and uppercase later: iPhone, eBay, macOS. Its own
# spelling, not a sentence that forgot its capital. Same rule as prose.py's _CAMEL_CASE.
_CAMEL_CASE = re.compile(r"^[a-z]\w*[A-Z]")


def _is_dangling(word: str) -> bool:
    """Whether a word cannot end a title. Punctuation is not part of the word."""
    return word.strip(".,;:!?").lower() in _DANGLING


def title_for(transcript: str | None) -> str | None:
    """
    A short title for one transcript, or ``None`` when there is nothing to name.

    ``None`` rather than a placeholder, so the caller can leave the column NULL and
    let every reader apply its own fallback -- which they already do, in SQL
    (``coalesce(title, summary, left(transcript, 80), 'Untitled memo')``) and in the
    browser (``memoLabel``). A titler that invented "Untitled memo" here would put a
    string in a column that three different readers are ready to handle as absent.

    :param transcript: The shaped transcript, as ``memo_ai.prose.shape`` left it.
        Unshaped input works and produces a worse answer -- the clause rules read
        punctuation, and an unpunctuated word stream has none.
    """
    if transcript is None:
        return None

    opening = _first_sentence(transcript.strip())

    if not opening:
        return None

    stripped = _without_fillers(opening)

    # Everything was filler: a memo of "Um, okay, so..." and nothing else. Nothing to
    # name, and better NULL than a title made of the words that were removed for
    # carrying no meaning.
    if not _WORD.search(stripped):
        return None

    named = _list_title(stripped)

    if named is not None:
        return named

    # The clause split first, then the trailing time, so the time rule sees whatever
    # survived rather than the whole sentence -- "call the dentist about the appointment
    # on Thursday" has already become "call the dentist" by the time it runs, and the
    # date it would have stripped is gone with the clause it belonged to.
    return _shorten(_without_trailing_time(_CLAUSE_BREAK.split(stripped, maxsplit=1)[0].strip()))


def _first_sentence(text: str) -> str:
    """
    Everything up to the first sentence-ending mark, or the whole thing.

    prose.shape has already decided where the sentences are -- it has the abbreviation
    and decimal-point rules, and a second, cruder copy of them here would disagree with
    it on exactly the inputs those rules exist for. So this splits on the marks that
    module has already placed, and its output on unshaped text is merely worse rather
    than wrong.
    """
    for at, character in enumerate(text):
        if character in ".!?…" and (at + 1 == len(text) or text[at + 1].isspace()):
            return text[:at]

    return text


def _without_fillers(text: str) -> str:
    """
    Drop leading filler and leading time, repeatedly, until the text starts with
    something meant.

    A loop rather than one pass, because openers stack in speech -- "Okay, so, note to
    self, ring the bank" is three of them in front of two words that matter, and
    "Tomorrow I will have a meeting" is a date followed by four function words. Bounded
    by the text getting shorter every iteration, so it terminates on any input.
    """
    while True:
        text = text.strip()

        phrase = next((f for f in _FILLERS if _opens_with(text.lower(), f)), None)

        if phrase is not None:
            # Past the filler, and past whatever punctuation separated it from the rest.
            # No floor here: filler is by definition contentless, so a memo made
            # entirely of it has nothing to name and title_for answers None.
            text = text[len(phrase) :].lstrip(" ,.:;-—…")

            continue

        time = _LEADING_TIME.match(text)

        if time is None:
            return text

        rest = text[time.end() :].lstrip(" ,.:;-—…")

        # **A floor, unlike the fillers above, and the difference is that a time is
        # content.** "Tomorrow morning." is a whole memo, and stripping its only words
        # as an "opener" left it with no title at all -- the date is not the frame
        # around the subject there, it *is* the subject. Found by running the rule over
        # a one-phrase memo; the same floor guards the trailing rule.
        if not _WORD.search(rest):
            return text

        text = rest


def _without_trailing_time(text: str) -> str:
    """
    Drop a time expression the memo ends on, repeatedly.

    Repeatedly, because these stack at this end too: "on Thursday morning at 3pm" is two
    of them, and removing one leaves a title ending in a date that was only there to
    introduce the clock.

    Never down to fewer than :data:`MIN_WORDS_AFTER_DATE` words. A memo that is *only* a
    time -- "Tomorrow morning." -- keeps it, and so does one where the date is most of
    what was said; the alternative is a one-word title that says less than the sentence
    it was cut from.
    """
    while True:
        shorter = _TRAILING_TIME.sub("", text).strip(" ,;:-—")

        if shorter == text or len(shorter.split()) < MIN_WORDS_AFTER_DATE:
            return text

        text = shorter


def _opens_with(lowered: str, filler: str) -> bool:
    """
    Whether the text starts with this filler *as a whole word or phrase*.

    The word-boundary check is the whole of this function and it is not decoration:
    without it "so" strips the first two letters of "Sort the invoices" and "hi" eats
    the front of "Hire a plumber". Checked against the following character rather than
    with a regex per filler, because there are forty of them and this runs on every
    memo.
    """
    if not lowered.startswith(filler):
        return False

    rest = lowered[len(filler) :]

    return rest == "" or not (rest[0].isalnum() or rest[0] == "'")


def _list_title(text: str) -> str | None:
    """
    "Shopping list", when the memo is one.

    The one rule here that replaces words rather than dropping them, so it is the one
    that has to be conservative: a leading verb from a five-word vocabulary *and* at
    least three items. Either alone is not enough -- "Buy the tickets" is not a list,
    and "milk, eggs and bread" with no verb might be a note about what somebody else
    bought.
    """
    words = text.split()

    if not words or words[0].strip(".,!?").lower() not in _LIST_VERBS:
        return None

    items = [part for part in _LIST_SPLIT.split(text) if _WORD.search(part)]

    return "Shopping list" if len(items) >= MIN_LIST_ITEMS else None


def _shorten(text: str) -> str | None:
    """
    Cap the title and give it a capital letter, ending on a word boundary.

    No ellipsis. A title is a label rather than an excerpt, and "Book tomorrow
    tickets…" claims to be a truncation of something the reader could go and see,
    which is misleading when it is a name this code invented. The transcript is one
    click away and is where the rest of the sentence lives.

    Trailing punctuation goes with the cut: a title that ends in a comma is a title
    that ends mid-breath.
    """
    words = text.split()

    if not words:
        return None

    kept: list[str] = []

    for word in words[:MAX_TITLE_WORDS]:
        candidate = " ".join([*kept, word])

        # The character cap wins over the word cap, except that one word always
        # survives -- a single 80-character word is a bad title and an empty one is
        # worse.
        if kept and len(candidate) > MAX_TITLE_CHARS:
            break

        kept.append(word)

    # **A cap that cut the sentence short ends the title at the last complete phrase.**
    #
    # Counting to six words lands wherever it lands, which is regularly in the middle of
    # a noun phrase: "Send the report to the finance team by Friday" capped at six is
    # "Send the report to the finance", which reads as a sentence that was interrupted
    # rather than as a name. Walking back to the last function word and cutting there
    # gives "Send the report to" and then, with the trailing dangler gone, "Send the
    # report" -- shorter, and a phrase.
    #
    # Only when something was actually dropped. A whole short sentence is already a
    # complete thought and must not be trimmed for ending in a preposition it needs.
    # Only from index 2 up, so this can never leave fewer than two words.
    if len(words) > len(kept):
        last = next(
            (at for at in range(len(kept) - 1, 1, -1) if _is_dangling(kept[at])),
            None,
        )

        if last is not None:
            kept = kept[:last]

    # Repeatedly, because a cut can leave two in a row -- "to the" at the end of the
    # example above. Guarded on there being something left, so a title made entirely of
    # function words comes back as None rather than as an empty string.
    while len(kept) > 1 and _is_dangling(kept[-1]):
        kept.pop()

    title = " ".join(kept).rstrip(" ,;:-—")

    if not title:
        return None

    # Only the first character, only if it is lowercase, and not at all when the word
    # is camel-cased. Nothing else is touched: a capital in the middle is far likelier
    # to be a name than a mistake, and a word that is lowercase first and uppercase
    # later -- iPhone, eBay, macOS -- is spelled the way its owner spells it. "IPhone
    # repair" is a worse title than "iPhone repair", and the same rule for the same
    # reason lives in prose.py's _capitalize.
    if not title[0].islower() or _CAMEL_CASE.match(title):
        return title

    return title[0].upper() + title[1:]

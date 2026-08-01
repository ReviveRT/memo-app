"""
Turning what the model said into something a person can read.

``"".join(segment.text)`` is what a whisper transcript is before this module, and
two separate things are wrong with it. Only one of them is whisper's fault.

**The join has no structure of its own.** However long the recording was, that
expression produces exactly one paragraph -- so a ninety-second memo arrives as an
unbroken block with nowhere for the eye to rest. Nothing about that depends on how
well the model punctuated; it is the only shape the join can produce.

**And sometimes the model punctuates nothing at all.** Whisper was trained on both
punctuated and unpunctuated transcripts, and which style it produces is a property
of the audio rather than a setting. It is not subtle when it goes the wrong way: a
real 89-second memo in this project's own database came back as 1204 characters of
lowercase words with not one comma in them, while every short memo recorded the
same day was punctuated correctly.

That second problem is **not fixed here, and cannot be.** It is fixed upstream, in
memo_ai/stt/local.py, by priming the decoder -- see ``PUNCTUATION_PRIMER`` there
for the measurements. Restoring punctuation from a bare word stream is a real
problem whose real solutions are all models, and guessing clause commas from
English word order with regular expressions produces confident nonsense. So this
module's job is the first problem plus the ordinary typographic tidying that a
punctuated transcript still needs:

  * whitespace and spacing around punctuation;
  * a capital at the start of every sentence, and the English pronoun ``I``;
  * a full stop on a transcript that ends without one;
  * paragraphs, so a long memo is not one block.

**The invariant, and it is the one worth knowing.** Nothing here adds, removes,
reorders or respells a word. Every rule below touches whitespace, punctuation or
letter case and nothing else, and ``test_the_words_are_never_touched`` asserts it
over every fixture in the suite -- because a formatter that quietly improved
somebody's wording would be indistinguishable from a transcription error, in the
one column of this application that is meant to be a record of what was said.

**Why the paragraphs are counted rather than heard.** The first version of this
read paragraph breaks off the pauses in the recording, which is much better
evidence than sentence-counting: a speaker who stops for two seconds has changed
subject. It was removed because the evidence is not there to read. Whisper's
segments tile the audio -- ``segment.end`` is the next segment's ``start`` -- so the
gap between them is always exactly ``0.00``. The segment boundaries are 30-second
window cuts and land mid-sentence, so they carry no structure either.

Stated as narrowly as it was actually measured, because the first version of this
paragraph claimed more: only two of the seven real recordings here have more than
one segment at all, the other five being short enough to decode as a single span and
so offering no gap to measure. Across those two -- one on each decode path, series
and batched -- run with and without the primer, that is 28 inter-segment gaps, every
one of them zero.

Genuine pause data does exist one level down, in ``word_timestamps=True``, and
whoever wants to reinstate the idea should start there. It was not taken now for
two reasons: it costs an extra alignment pass on every memo, and the primer that
this module's docstring points at is already proof that perturbing the decode
options can reintroduce the repetition loop MEMO-14 fixed. Sentence counting needs
neither risk, and on a punctuated transcript it lands the breaks between whole
sentences either way.
"""

import re
from math import ceil

# What can end a sentence, once it has survived the guards in _ends_sentence. The
# single-character ellipsis is included because whisper emits it as well as three
# dots.
TERMINATORS = ".!?…"

# The most sentences a paragraph may hold. Past this the transcript is divided into
# as few equal runs as will fit -- see _paragraphs.
#
# Five, which is the upper end of what style guides suggest rather than the middle
# of it. Every break this module places is a guess from a sentence count, so the
# number that produces the *fewest* of them while still breaking up a wall is the
# conservative choice. It leaves a memo of five sentences or fewer -- which is most
# of them -- as the single paragraph it already was.
PARAGRAPH_MAX_SENTENCES = 5


def shape(text: str, language: str | None = None) -> str:
    """
    The readable form of one transcript.

    ``language`` is whisper's own answer for the recording, and the only thing it
    gates is the ``i`` rule in :func:`_capitalize`, which is wrong in several
    languages where a bare ``i`` is a real word. ``None`` means unknown, and the
    unknown case declines the rule rather than guessing.

    Returns ``""`` for input with no words in it, which is what the local provider
    turns into its own "no speech" error rather than writing a blank memo.
    """
    tidied = _hygiene(text)

    # "No words in it" rather than "empty", and the difference is reachable: a
    # transcript of "..." or "," survives hygiene, and everything below would
    # dutifully capitalize and terminate it into "..." or ".". Neither is speech, and
    # the provider's "no speech" error is a better answer than a memo containing one
    # mark. `\w` rather than a letter test, so a memo that is only the number
    # somebody said still counts.
    if not _WORD.search(tidied):
        return ""

    sentences = [_capitalize(s, language) for s in _sentences(tidied)]

    return "\n\n".join(" ".join(run) for run in _paragraphs(sentences))


_WORD = re.compile(r"\w")

# Every whitespace run collapses to one space, newlines included. Line breaks in a
# transcript are this module's to place: whisper's segment text has none, and a
# hosted provider's would reflect its own chunking rather than anything about the
# speech.
_WHITESPACE = re.compile(r"\s+")

# Space stranded before punctuation that closes. Whisper produces this at a
# segment join often enough to matter -- one segment ending mid-clause and the next
# opening with ", and" meet as "... , and".
#
# Two rules rather than one, and the full stop is the reason. A lone dot after a
# space is a stray space; a *run* of them is an ellipsis, and the space in front of
# it is likely deliberate -- "Wait! ...then he left" is a continuation, and closing
# that space up gives "Wait!...then". So the run is left alone, and `…` with it, for
# the same reason. Found by fuzzing.
#
# The lookahead still lets a *spaced* run close up one dot at a time: in "a . . b"
# each dot is lone at the moment it is examined, so both spaces go and _DOUBLED gets
# the ".." it needs.
_SPACE_BEFORE_CLOSE = re.compile(r"\s+([,;:!?%)\]}])")
_SPACE_BEFORE_STOP = re.compile(r"\s+(\.)(?!\.)")

_SPACE_AFTER_OPEN = re.compile(r"([(\[{«])\s+")

# A comma, semicolon or colon with no space after it, and only in front of a
# letter. The digit case is excluded because it is not an error: `1,000` and
# `12:30` are correct as written, and "fixing" them corrupts a number -- which is
# the part of a memo most likely to be a fact somebody needs.
_MISSING_SPACE = re.compile(r"([,;:])(?=[^\W\d])")

# A terminator with a capitalized word welded to it, which is a missing space
# rather than an abbreviation. The lowercase letter after the capital is what makes
# it safe: it tells "body.Postgres" from an acronym like "U.S.A".
#
# It runs before sentence splitting on purpose, and _ends_sentence depends on it
# having done so -- that function treats a terminator with no space after it as
# proof of a word rather than a boundary, which is only true once this rule has
# separated the cases where a boundary is what it really was.
_MISSING_SENTENCE_SPACE = re.compile(r"([.!?…])(?=[A-Z][a-z])")

# Two dots, or four and up, normalized to one and to three. Exactly three is left
# alone: it is an ellipsis, whisper means it, and it carries a "trailed off" that a
# full stop would claim was a finished thought.
_DOUBLED = re.compile(r"(?<!\.)\.\.(?!\.)")
_OVERLONG = re.compile(r"\.{4,}")


def _hygiene(text: str) -> str:
    """
    The spacing rules, which are the uncontroversial half of this module.

    Ordered rather than independent, and the order was wrong to begin with. The dot
    runs used to be normalized first, which left the contract they exist to provide
    -- that everything downstream sees one dot or exactly three -- false for the one
    input that needed it: "a . . b" had its spaces closed up *after* the run
    normalizer had already looked, and reached sentence splitting as "a.. b".
    Closing the spaces first is what makes a spaced run a run at all.
    """
    text = _WHITESPACE.sub(" ", text).strip()
    text = _SPACE_BEFORE_CLOSE.sub(r"\1", text)
    text = _SPACE_BEFORE_STOP.sub(r"\1", text)
    text = _OVERLONG.sub("...", text)
    text = _DOUBLED.sub(".", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _MISSING_SPACE.sub(r"\1 ", text)

    return _MISSING_SENTENCE_SPACE.sub(r"\1 ", text)


# Words that end in a full stop without ever ending a sentence.
#
# **Every entry here is a word that can now never end a sentence**, so the only ones
# that belong are those which are not English words in their own right. That rule
# was written before this list was, and then the list broke it: an earlier version
# carried the months and weekdays, and `sat`, `sun`, `wed` and `mar` are all
# ordinary words. The cost was not subtle -- "he sat. then he stood" came out as one
# sentence with a lowercase "then" in the middle of it, because a suppressed
# boundary also suppresses the capital after it.
#
# So the days and months are gone. Whisper writes "Saturday" rather than "Sat." when
# somebody says it, which made them speculative as well as harmful. What is left is
# the titles, which whisper really does emit, and a handful of abbreviations that are
# not words. `no` is absent for the same reason and has always been: "No." as a whole
# answer is commoner in speech than "no." abbreviating "number" -- see _NUMBERED,
# which is where that case is handled properly.
_ABBREVIATIONS = frozenset(
    """
    mr mrs ms mx dr prof rev sr jr st
    vs etc eg ie cf approx dept
    inc ltd co corp
    """.split()
)

# Labels that abbreviate only when a number follows them.
#
# The distinction this draws is what the single list above could not. "fig" is a
# fruit and "no" is an answer, so neither can be suppressed unconditionally -- but
# "fig. 2" and "no. 5" are not sentence ends either, and the numeral after them is
# what says so. Several of these are ordinary words, and that is precisely why they
# are in this list rather than the other one.
_NUMBERED = frozenset("no nos fig figs vol vols pp ch sec art ref para eq tab".split())

# The word immediately before a dot, for the abbreviation and initial tests.
#
# Apostrophes are part of the word, and leaving them out was a bug with a wide blast
# radius: matching letters alone, "don't." ends in the single letter `t`, which the
# initial test below reads as the `p` of "p.m." and so refuses to end the sentence.
# Every `n't` contraction in English is affected -- don't, can't, won't, isn't,
# didn't, couldn't -- and they are among the commonest words in dictated speech, so
# "I don't. It's fine." came out as one sentence with a lowercase "it's" in it.
#
# Both apostrophes, because whisper emits the straight one and a hosted provider may
# well send the typographic one.
_BEFORE_DOT = re.compile(r"([A-Za-z'’]+)$")

# The apostrophes, for measuring a word's length in letters. "don't" is a five-letter
# word for the purposes of the set lookups and a four-letter one for the initial
# test; what that test is really asking is "is this a single letter", and an
# apostrophe is not one.
_APOSTROPHES = re.compile(r"['’]")

_TERMINATOR_RUN = re.compile(f"[{re.escape(TERMINATORS)}]+")

# Marks that close a sentence from the outside, and so belong to the sentence
# ending rather than the one starting. A cut landing in front of one leaves it
# opening the next sentence: "He said 'go. ' Then left."
_CLOSERS = "\"'»)]}"

# A comma, semicolon or colon stranded at the front of a sentence. It gets there
# when the model ends a sentence at the end of one segment and opens the next with
# a continuation -- "... checked the body." then ", and Postgres is on the named
# volume" -- so the two marks meet with the weaker one second. Dropped, because no
# sentence begins with a comma.
#
# Applied per sentence rather than by one regex over the whole text, which was the
# first version and is not safe: a regex cannot tell this case from the legitimate
# comma in "at 12 p.m., and then", where the full stop belongs to the abbreviation.
# By the time a sentence has been cut out, the guards below have already run, so a
# comma at its front really does follow a terminator.
_CONTINUATION = re.compile(r"^[,;:]\s*")

# A sentence being closed by this module that ends on a mark which cannot close
# one.
_DANGLING = re.compile(r"[,;:]$")


def _ends_sentence(text: str, match: re.Match[str]) -> bool:
    """
    Whether a run of terminators in ``text`` really ends a sentence.

    ``!`` and ``?`` always do. Two other cases do not.

    **An ellipsis does not end a sentence.** ``...`` in a transcript is hesitation
    -- the speaker trailing off and carrying on -- so cutting there would invent a
    boundary and, worse, capitalize the word after it, turning "I was thinking...
    maybe not" into two half-sentences.

    **A full stop is only sometimes one.** Four ways it can be something else, all
    decidable from the characters around it: another word joined straight onto it
    (``index.html``), an initial or abbreviation in front of it (``12 p.m.``,
    ``Dr.``), a decimal point (``1.5``), or a numbered reference (``no. 5``).
    """
    run = match.group()

    # What follows, past anything belonging to the sentence just ended rather than
    # the next one: a closing quote or bracket, and a comma or semicolon left
    # against the terminator by a segment join. `_CONTINUATION` drops that stray
    # mark later; here it only has to not hide the boundary.
    after = text[match.end() :].lstrip(_CLOSERS + ",;:")

    # Nothing after it ends the sentence. Anything that is not a space means the
    # mark is inside a word rather than after one -- the test that keeps
    # "index.html", verbatim from a real memo here, in one piece. It is safe only
    # because _MISSING_SENTENCE_SPACE has already put a space into the genuine
    # "body.Postgres" case before this runs.
    if after and not after[:1].isspace():
        return False

    if run != ".":
        # Dots and `…` in any combination are the ellipsis case; a run containing a
        # `!` or `?` is a terminator. (`..` cannot reach here -- _hygiene has
        # already made it one dot.)
        return not set(run) <= set(".…")

    word = _BEFORE_DOT.search(text[: match.start()])

    if word is None:
        # Nothing lettered in front of it: a bare "42." or "12:30.", an ordinary
        # end of sentence. "1.5" cannot reach here, having failed the whitespace
        # test above with a digit welded to its dot.
        return True

    label = _APOSTROPHES.sub("", word.group(1)).lower()

    # An initial ("12 p.m.") or an abbreviation that is never a word ("Dr."). Empty
    # when the "word" was nothing but apostrophes, which is not an initial either.
    if len(label) == 1 or label in _ABBREVIATIONS:
        return False

    # "no. 5", "fig. 2" -- a label that abbreviates only because a numeral follows
    # it. Both halves are required, and the earlier version of this tested only the
    # numeral: any word at all followed by a digit stopped ending a sentence, so
    # "that is done. 38 memos survived" was one sentence and the paragraph rule
    # counted it as one.
    return not (label in _NUMBERED and after.lstrip()[:1].isdigit())


def _sentences(text: str) -> list[str]:
    """
    Cut one tidied transcript into sentences.

    The last one is closed by this function if the model left it open, which is
    why a memo that came back as "I would like to place an order" now ends in a
    full stop. Nothing else is inserted: a sentence boundary the model did not
    punctuate is one this module has no evidence for -- see the note on pauses in
    the module docstring.
    """
    out: list[str] = []
    cut = 0

    for match in _TERMINATOR_RUN.finditer(text):
        if match.end() <= cut or not _ends_sentence(text, match):
            continue

        # Past the closing quote or bracket, so it stays with the sentence it
        # closes. The `match.end() <= cut` guard above is what that skipping makes
        # necessary: a terminator inside text already consumed must not open a
        # sentence starting before `cut`.
        end = match.end()

        while end < len(text) and text[end] in _CLOSERS:
            end += 1

        sentence = _CONTINUATION.sub("", text[cut:end].strip())
        cut = end

        if sentence:
            out.append(sentence)

    tail = _CONTINUATION.sub("", text[cut:].strip())

    if tail:
        out.append(_terminate(tail))

    return out


def _terminate(sentence: str) -> str:
    """
    Close the last sentence, which the model left open.

    A trailing comma is *replaced* rather than followed, because "and then," with a
    full stop bolted onto it reads as a typo. That is the only punctuation this
    module removes, and only from a position where it cannot be right.

    Text already ending in an ellipsis keeps it and gets nothing added: the speaker
    trailed off and then stopped, which "..." says and "...." does not.
    """
    if sentence.endswith(tuple(TERMINATORS)):
        return sentence

    return _DANGLING.sub("", sentence) + "."


# The first word of a sentence, past whatever opens it -- a quote, a bracket, the
# inverted marks Spanish opens a question with. The whole word is captured rather
# than its first letter, because whether it may be capitalized depends on the rest
# of it.
_OPENING_WORD = re.compile(r"^\W*(\w+)")

# A word that is lowercase first and uppercase later: iPhone, eBay, macOS. Capitals
# inside a word are deliberate, so the lowercase one starting it is deliberate too,
# and "IPhone" is a worse sentence opening than "iPhone".
_CAMEL_CASE = re.compile(r"^[a-z]\w*[A-Z]")

# A lone `i`, the English pronoun. `\bi\b` also matches the i in "i.e.", so a
# following dot is excluded; "i'm" and "i'll" are wanted, and an apostrophe is a
# word boundary, so those are reached.
_LONE_I = re.compile(r"\bi\b(?!\.)")


def _capitalize(sentence: str, language: str | None) -> str:
    """
    A capital at the start, and the English pronoun.

    The ``i`` rule is gated on the language for a reason easy to miss from an
    English keyboard: a bare ``i`` is an ordinary word in several languages whisper
    transcribes -- "and" in Polish, a plural article in Italian -- and capitalizing
    it there is not a fix but a typo introduced into every sentence containing one.

    Two openings are left alone. A sentence starting with a *number* has no letter
    to capitalize, and reaching past the digits for one turns "12 p.m. is fine"
    into "12 P.m. is fine", which is what the first version of this did. And a
    camel-cased word is already spelled the way its owner spells it.

    Nothing here lowercases anything: a capital mid-sentence is far likelier to be
    a name than a mistake.
    """
    if language == "en":
        sentence = _LONE_I.sub("I", sentence)

    opening = _OPENING_WORD.search(sentence)

    if opening is None:
        return sentence

    word = opening.group(1)
    at = opening.start(1)

    if not word[:1].isalpha() or _CAMEL_CASE.match(word):
        return sentence

    # A word welded to a terminator is a continuation, not an opening. This only
    # happens when the sentence itself begins with terminators -- "...then he left"
    # after a "Wait!" -- and leaving it lowercase is what _ends_sentence already
    # decided about an ellipsis: it interrupts a sentence rather than starting one.
    #
    # It is also the one place these rules could contradict each other. Capitalizing
    # here produces "...Then", which is exactly the pattern _MISSING_SENTENCE_SPACE
    # inserts a space into -- so shaping the same text twice gave "... Then" and
    # shaping was not idempotent, which test_shaping_is_idempotent asserts and
    # MEMO-16's retry relies on.
    if at and sentence[at - 1] in TERMINATORS:
        return sentence

    return sentence[:at] + word[0].upper() + sentence[at + 1 :]


def _paragraphs(sentences: list[str]) -> list[list[str]]:
    """
    Divide the sentences into as few equal runs as :data:`PARAGRAPH_MAX_SENTENCES`
    allows.

    Equal runs rather than filling each paragraph to the cap before starting the
    next, and the difference shows on exactly the input that needs it most. Six
    sentences greedily filled are a paragraph of five and an orphan of one; divided,
    they are two of three. Since the break positions are a guess either way, the
    arrangement that does not produce orphans is the better guess.
    """
    if len(sentences) <= PARAGRAPH_MAX_SENTENCES:
        return [sentences]

    parts = ceil(len(sentences) / PARAGRAPH_MAX_SENTENCES)
    size, over = divmod(len(sentences), parts)
    out = []
    at = 0

    for index in range(parts):
        # The first `over` runs take the remainder, one sentence each, so the runs
        # differ by at most one.
        take = size + (1 if index < over else 0)
        out.append(sentences[at : at + take])
        at += take

    return out

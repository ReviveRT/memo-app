"""
What kind of thing went wrong, as a short token beside the sentence saying it.

``memos.last_error`` holds prose, written for the person who made the recording and
worded by whichever module detected the fault. That is the right shape for a human
and the wrong shape for a program: the frontend has to treat "there was nothing in
this recording" differently from "the model could not be loaded" -- one is a memo
worth keeping and retrying, the other is an empty file the app should not have made
a card for -- and the only way to tell them apart from the sentence alone is to
match on its text. Prose gets reworded. A frontend keyed to a substring of it breaks
silently, and the symptom is memos that quietly stop being tidied up, or the wrong
ones being deleted.

So the classification travels as its own value. ``memos.last_error_code`` is written
by the same statement that writes the sentence, and the two always agree because
neither is derived from the other -- both come from the raise site, which is the only
place that knows.

**The vocabulary is closed and every failure path sets one.** A NULL code on a
``failed`` row would mean "some fault nobody classified", which is a thing a reader
would then have to guess about; :data:`UNEXPECTED` exists so that case has a name.
The column is nullable only because a memo that has never failed has no code, and
because the migration adds it to rows that predate it.

No CHECK constraint on the column, deliberately, unlike ``source`` and ``status``
next to it. Those two are the memo's own lifecycle and a new value is a schema
change on purpose; this is a *diagnosis*, and a new provider finding a new way to
fail should not need a migration to say so. The cost is that a typo here reaches the
database, which is why these are constants rather than literals at the raise sites.
"""

# --------------------------------------------------------------------------
# There was nothing to transcribe
# --------------------------------------------------------------------------
#
# The two codes in DISCARDABLE below, and what unites them is not "the attempt
# failed" but "the recording has no content in it". Nothing was lost, because there
# was nothing there: no words were said, or there was no audio at all.

#: The file decoded fine and contained no speech. Silence, a muted microphone, or a
#: recording cut short before anyone spoke.
NO_SPEECH = "no_speech"

#: There is no audio to transcribe -- a container with no audio track, or one whose
#: audio measures zero length.
NO_AUDIO = "no_audio"

# --------------------------------------------------------------------------
# Something went wrong with a real recording
# --------------------------------------------------------------------------

#: Longer than ``MAX_AUDIO_SECONDS``. Refused before any provider ran, and refused
#: the same way every time -- so it is terminal, but the recording is real.
TOO_LONG = "too_long"

#: ffmpeg or the model could not read the file. Real audio, unusable copy.
UNREADABLE = "unreadable"

#: The provider cannot run here and now -- a model still downloading, a load that ran
#: out of memory, a provider that is named in the configuration but not built. The
#: recording was never looked at, which is what makes this the most retryable of them.
PROVIDER_UNAVAILABLE = "provider_unavailable"

#: Transcription ran past its deadline and was stopped.
TOO_SLOW = "too_slow"

#: The claim expired while the memo was being worked on and it went back to the queue.
#: Not a fault of the recording at all; see REAPED_MESSAGE in memo_ai/memos.py.
INTERRUPTED = "interrupted"

#: Interrupted once too often and given up on, with no reason of its own ever recorded.
ABANDONED = "abandoned"

#: A transcription failure the provider classified as terminal but did not name more
#: precisely. The fallback for :class:`~memo_ai.stt.base.SttError` itself.
TRANSCRIPTION_FAILED = "transcription_failed"

#: An exception nobody classified. The row carries a generic sentence and the
#: traceback is in the log -- see UNEXPECTED_ERROR in memo_ai/pipeline.py.
UNEXPECTED = "unexpected"


#: The failures that mean the recording had nothing in it.
#:
#: The frontend deletes these rather than showing them: a memo whose whole content is
#: "you did not say anything" is not a memo, and leaving a card for it makes the list
#: a list of the user's mistakes. Everything else stays, with a Retry button, because
#: the recording is real and the fault may be fixable.
#:
#: web/src/memoFailure.js holds the other copy of this set. Two runtimes cannot share
#: a constant, so each names the other -- the same arrangement MemoDialog and
#: UpdateMemoRequest use for the title cap. Keeping the *codes* in step is the easy
#: half of that; it is the sentences that could not have been.
DISCARDABLE = frozenset({NO_SPEECH, NO_AUDIO})

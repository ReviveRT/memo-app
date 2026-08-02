/*
 * Why a memo failed, in the words the worker already wrote.
 *
 * **The bug this exists for was a silent one, and everything needed to fix it was already on
 * the wire.** Record four seconds of silence, or a cough, or hold the microphone somewhere
 * that never hears you, and the worker refuses the memo with a real sentence -- "No speech was
 * detected in this recording. It may be silent, too quiet for the microphone that captured it,
 * or cut short before anything was said." That goes into `memos.last_error`, `MemoRepository`
 * projects the column, and the API sends it on every row.
 *
 * Nothing rendered it. The card said "Could not transcribe" over "No transcript yet.", and the
 * detail card said "This recording could not be transcribed." Both are restatements of the
 * FAILED badge sitting next to them, so the whole screen said the same content-free thing three
 * times and the one sentence that explained it was thrown away between the response and the
 * DOM. There was no way to tell "you were too quiet" from "ffmpeg could not read this" from
 * "the model timed out" -- and the first of those is the user's to fix in two seconds.
 *
 * So this is a getter with a fallback, not a message table. The wording belongs to whoever
 * detected the fault: memo_ai/stt/local.py has the three no-speech causes in one sentence
 * because it is the code that knows there are three, and memo_ai/audio.py words its own. See
 * pipeline.py's UNEXPECTED_ERROR for the rule the worker applies about what may go in the
 * column at all -- an unclassified exception's text never does, so what arrives here was
 * written to be read by a person.
 *
 * `canRetry` joined it for MEMO-17 and belongs in the same file rather than beside the button,
 * because the two are one idea: a failed memo is only *visible* if there is a reason, and only
 * *recoverable* if there is an action, and the three places that render one of those render
 * both. Keeping them together is what stops a card offering Retry over a memo it is describing
 * as finished.
 */

/**
 * The reason a memo failed, or null if it did not fail or the worker left no reason.
 *
 * `status === 'failed'` is required as well as the column being set, because `last_error` is
 * not cleared on a retry that then succeeds -- it is the last error, not the current state.
 * Reading the column alone would put a stale explanation under a memo that has since
 * transcribed fine.
 *
 * @param {object} memo
 * @returns {?string}
 */
export function failureReason(memo) {
  if (memo?.status !== 'failed') {
    return null
  }

  return typeof memo.last_error === 'string' && memo.last_error.trim() !== ''
    ? memo.last_error.trim()
    : // A failed memo with an empty column. Not reachable through the worker, which writes one
      // on every failure path, and reachable through a row written by hand or by a future
      // writer -- so it says that it does not know rather than leaving the card blank, which is
      // the state this whole module exists to remove.
      'This memo could not be transcribed, and no reason was recorded.'
}

/**
 * The failure codes that mean the recording had nothing in it.
 *
 * The other copy of `DISCARDABLE` in ai/memo_ai/failures.py, which is where the vocabulary is
 * defined and argued for. Two runtimes cannot share a constant, so each names the other -- the
 * same arrangement MemoDialog and UpdateMemoRequest use for the title cap, and a much safer one
 * here than it looks: these are short tokens chosen to be stable, unlike the sentences beside
 * them, which exist to be reworded and are the reason the codes exist at all.
 *
 * A code this set has never heard of keeps the memo. That is the safe direction for an unknown
 * value to fall, and it is why this is a set of things to *discard* rather than a set of things
 * to keep: a worker newer than this bundle can invent a failure kind, and the worst that does
 * is leave a card that could have been tidied away.
 */
const DISCARDABLE_CODES = new Set(['no_speech', 'no_audio'])

/**
 * Whether this memo is an empty recording -- one the app should not keep a card for.
 *
 * **The rule: a memo whose whole content is "you did not say anything" is not a memo.** A
 * silent recording, a muted microphone, a file with no audio track -- there is nothing in it to
 * transcribe now and nothing a retry could find later, so a `failed` card for it is a permanent
 * receipt for a misfire, sitting in the list next to real memos and needing to be deleted by
 * hand. useMemoList deletes these instead of rendering them, and the toast says why.
 *
 * **Keyed on the code and never on the sentence.** `last_error` is prose written for a person
 * and it gets reworded; a branch keyed to a substring of it breaks silently, and the two ways
 * it can break are "memos quietly stop being tidied up" and "the wrong ones start being
 * deleted". db/migrations/004_last_error_code.sql has the full argument for the column.
 *
 * `status === 'failed'` as well as the code, for the reason `failureReason` needs it too: the
 * pair is not cleared when a retry succeeds, so a memo that failed with no speech and then
 * transcribed on a second attempt still carries `no_speech` while being perfectly `ready`.
 * Reading the code alone would delete it.
 *
 * @param {object} memo
 * @returns {boolean}
 */
export function isEmptyRecording(memo) {
  return memo?.status === 'failed' && DISCARDABLE_CODES.has(memo.last_error_code)
}

/**
 * Whether this memo can be sent back to the worker (MEMO-17).
 *
 * The other half of the same idea: a reason nobody can act on is only half an improvement, and
 * most of the sentences on the other side of `failureReason` describe something the person
 * reading them can fix. A key that was not set, a model that had not finished downloading, a
 * microphone that was muted -- the worker's own retries all happen within a couple of minutes
 * of the recording, so by the time anyone has read the reason and done something about it the
 * memo is terminal and nothing left in the stack will touch it again. This is what puts it back
 * in play.
 *
 * `failed` and nothing else, which is the API's rule rather than a second one invented here --
 * MemoRepository::requeue guards the UPDATE on the same status and answers 409 otherwise, and
 * there are real hazards behind that (requeueing a `processing` row puts two workers on one
 * memo). So this is the *button's* predicate: it decides whether to offer the action, and the
 * server decides whether to perform it. They agree, and when they disagree -- a card holding a
 * status the worker has since moved on from -- the 409's sentence is what the user sees.
 *
 * Deliberately not `failureReason(memo) !== null`, even though the two nearly answer the same
 * today. That one is about having something to *say* and falls back to a sentence when the
 * column is empty; this is about what may be *done*, and reading a button's availability out of
 * a message's presence is the kind of coupling that turns a future empty-string fallback into a
 * disabled control.
 *
 * **Empty recordings are excluded, and it is not only that they are about to be deleted.**
 * Retrying one is a guaranteed round trip to the same answer -- the file is unchanged, and
 * "there is no speech in it" is a property of the file rather than of the attempt. So the
 * button would be a lie even on the copy that survives, which is the one whose delete failed:
 * for that memo the honest action is Delete, which the detail card already offers.
 *
 * @param {object} memo
 * @returns {boolean}
 */
export function canRetry(memo) {
  return memo?.status === 'failed' && !isEmptyRecording(memo)
}

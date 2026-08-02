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

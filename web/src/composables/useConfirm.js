import { ref } from 'vue'

/*
 * "Are you sure?", as a question this app asks rather than one the browser does.
 *
 * **Why `window.confirm` had to go.** It was the right first choice and it stopped being one.
 * Its advantages are real -- it is one line, it is modal for free, and it cannot be styled into
 * something misleading -- but four things count against it here:
 *
 *   * **It looks like the browser, not like the app.** A Chrome sheet that says
 *     "localhost:5173 says" over a dark, deliberate interface reads as a warning *about* the
 *     page rather than a question *from* it.
 *   * **It cannot say which button is the dangerous one.** OK and Cancel, in that order, in
 *     the browser's colours. The destructive action and the safe one are indistinguishable
 *     until the sentence is read, and it is the action most worth marking.
 *   * **It blocks the main thread.** Everything stops while it is open: the poll, the toast
 *     bars, the bloom. A memo mid-transcription visibly freezes behind it.
 *   * **It cannot carry structure.** The wording here names what else a delete removes -- the
 *     recording, the reminders -- and one run-on sentence is the only shape available.
 *
 * **Why a promise and not a component with props.** The call sites read the way they did
 * before: `if (!(await ask(...))) return`. A pair of `open` and `@confirm` props would turn
 * one guarded statement into a state flag, a handler and a second function holding what to do
 * afterwards -- in two components, for one question each. The promise keeps the decision and
 * the action in the same three lines.
 *
 * One host renders it. `ConfirmDialog` is mounted once by MemosView and reads this module; a
 * dialog per caller would mean two `<dialog>` elements racing for the top layer.
 */

/**
 * The question on screen, or null when nothing is being asked.
 *
 * @type {import('vue').Ref<?{title: string, body: ?string, confirmLabel: string, danger: boolean}>}
 */
const question = ref(null)

/**
 * How to answer the promise the caller is waiting on.
 *
 * @type {?(answer: boolean) => void}
 */
let settle = null

/**
 * Ask, and resolve with what they chose.
 *
 * A second call while one is open answers the first with `false` rather than queueing or
 * ignoring it. Not reachable through the UI -- the dialog is modal, so nothing behind it can be
 * clicked -- and the alternative is worse than the case is likely: an unanswered promise leaves
 * the caller's `await` suspended for the life of the page, which from the screen is a button
 * that did nothing. The same rule useRecorder applies to its own stop().
 *
 * @param {{title: string, body?: ?string, confirmLabel?: string, danger?: boolean}} asked
 *   `title` is the question. `body` is what the reader needs to decide it -- what else goes,
 *   what cannot be undone -- and is deliberately separate so it can be a second line rather
 *   than a longer sentence.
 * @returns {Promise<boolean>}
 */
export function ask({ title, body = null, confirmLabel = 'Confirm', danger = false }) {
  settle?.(false)

  question.value = { title, body, confirmLabel, danger }

  return new Promise((resolve) => {
    settle = resolve
  })
}

/** Answer the open question. Called by the dialog, and by Escape and the backdrop. */
export function answer(agreed) {
  const settled = settle

  settle = null
  question.value = null
  settled?.(agreed)
}

export function useConfirm() {
  return { question, ask, answer }
}

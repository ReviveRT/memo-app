import { ref, watch } from 'vue'
import { failureReason } from '../memoFailure'
import { memoLabel } from '../memoLabel'

/*
 * One card in the corner per memo being written, from the button press to the transcript.
 *
 * **What this replaces, and why the pieces that were already there were not enough.** Three
 * things reported a memo in flight before this: a determinate bar under the recorder while the
 * bytes go out, a 4px estimate bar on the memo's own card, and a "Still transcribing" line
 * under the composer. Each is correct and each is easy to miss.
 *
 *   * The upload bar is measured in milliseconds on localhost -- the API is one container away
 *     -- so the one part of the wait that has a real number attached is over before it can be
 *     read. Then it disappears, and the next thing that happens is nothing, for several
 *     seconds, in the place the user is looking.
 *   * The card's bar is on the card, and the card is in a strip that scrolls sideways, under a
 *     composer, in a page that scrolls. A memo submitted from the bottom of the collections
 *     grid draws its progress somewhere off screen.
 *   * None of them survives the memo finishing. `ready` arrives, the bar vanishes, and nothing
 *     ever says the transcript is there -- which is the moment the whole wait was for.
 *
 * So this is one fixed thing, in one place, that follows a single submission through every
 * stage it has and then says how it ended. It does not replace the card's bar: that one
 * belongs to the row and answers "is *this* memo still working", which is a different question
 * once there are five of them.
 *
 * **The phase is stored; the bar is not.** A toast holds which stage it is in and nothing about
 * how full a bar should be, because the two stages that have a bar get their number from
 * somewhere else entirely -- the upload from XHR's progress events, the transcription from
 * useProcessingProgress's estimate. Storing a fraction here would mean copying a number that
 * changes four times a second into reactive state on every tick, and it would let the toast's
 * bar and the card's bar disagree about the same memo. The component reads both sources
 * directly; see MemoToasts.vue.
 *
 * **Nothing here knows about reminders,** which have their own delivered-toast list in
 * useReminders. They share the corner and the stylesheet and nothing else: a reminder is
 * something the user asked for at a time they chose, and this is a receipt for something they
 * did two seconds ago. Merging the two lists would mean one dismiss-all button clearing both.
 */

/**
 * How long a finished memo's toast stays up.
 *
 * Long enough to read the title that was just generated for it, short enough that a burst of
 * memos does not bury the corner. Failures are not on a timer at all -- see `settle`.
 */
const READY_DISMISS_MS = 6_000

/**
 * The stages a submission goes through, in order.
 *
 * `uploading` and `saving` are the two shapes of the same stage -- bytes going out -- and they
 * are separate values rather than one because only the first of them has a number to show. A
 * typed memo's POST is a few hundred bytes and reports nothing worth a bar.
 *
 * `queued` and `processing` are the API's own statuses, carried through unchanged so that this
 * file has no opinion about what the worker is doing. Anything else the server ever reports is
 * treated as still working, which is the rule useMemoList states at TERMINAL_STATUSES and the
 * reason it is stated as a negation there.
 */
const PHASES = {
  UPLOADING: 'uploading',
  SAVING: 'saving',

  /**
   * The third shape of "bytes going out", and the one that is not a submission at all: a
   * failed memo being handed back to the worker (MEMO-17).
   *
   * It gets its own value rather than borrowing SAVING because the wording has to differ --
   * nothing is being saved, the memo has existed for some time -- and because it is the stage
   * a retry can be *refused* in, which a submission cannot be. Everything after it is shared:
   * once the API answers, a retried memo is a memo in the queue and there is nothing left to
   * tell apart. That is the whole reason this hooks into the toasts rather than growing a
   * second mechanism beside them.
   */
  RETRYING: 'retrying',

  QUEUED: 'queued',
  PROCESSING: 'processing',
  READY: 'ready',
  FAILED: 'failed',

  /** The write itself did not land: no row, no memo id, nothing to watch. */
  REJECTED: 'rejected',
}

/**
 * Which stage each control opens on, before the API has answered anything.
 *
 * A table rather than the ternary this used to be, because there are three of them now and a
 * nested conditional over a string is where a fourth one gets added wrong. The `?? SAVING`
 * fallback at the call site keeps an unknown kind rendering something rather than a blank card.
 */
const OPENING_PHASE = {
  voice: PHASES.UPLOADING,
  text: PHASES.SAVING,
  retry: PHASES.RETRYING,

  // The odd one out: it opens on its *last* stage, because a discard is only ever
  // reported after the fact. reportDiscarded settles it in the next statement anyway, so
  // this is never rendered -- it is here so the toast is never briefly in a phase that
  // claims work is happening, and so that `kind` reads as what it is at the one place
  // that looks at it (the REJECTED title, which a discard cannot reach).
  discard: PHASES.FAILED,
}

const TERMINAL_STATUSES = new Set(['ready', 'failed'])

/** @type {import('vue').Ref<Array<object>>} */
const toasts = ref([])

/** Ids handed out in order, so two toasts created in the same millisecond cannot collide. */
let nextId = 0

/**
 * Stop handles and dismiss timers, keyed by toast id.
 *
 * Outside the toast objects themselves, because they are rendered: a `watch` stop handle on a
 * reactive object is a function Vue would walk on every update, and it has no business being
 * reachable from a template.
 *
 * @type {Map<number, {stopWatching: ?() => void, dismissTimer: ?number}>}
 */
const machinery = new Map()

/**
 * Start following one submission.
 *
 * Called by useMemos at the top of the write path, before the request goes out, so the corner
 * says something from the first frame rather than from whenever the server answers.
 *
 * Also called when a failed memo is sent back to the worker, which is not a submission but is
 * the same wait with the same three endings -- see PHASES.RETRYING.
 *
 * @param {'voice'|'text'|'retry'|'discard'} kind Which control started this. Decides the opening
 *   wording, whether the first stage draws a bar, and how a rejection is described. `discard` is
 *   not a control at all -- see reportDiscarded, which reports something that already happened.
 * @returns {{
 *   uploading: (fraction: number) => void,
 *   stored: (memo: object) => void,
 *   rejected: (message: string) => void,
 * }}
 */
export function startMemoToast(kind) {
  const id = ++nextId

  toasts.value = [
    ...toasts.value,
    {
      id,
      phase: OPENING_PHASE[kind] ?? PHASES.SAVING,

      /**
       * Kept on the toast, and read by exactly one thing: what to call a rejection.
       *
       * `phase` cannot answer it. REJECTED means "the write did not land", which is true of a
       * memo that was never stored and of a retry that was refused, and "Not saved" under the
       * second one describes something nobody was trying to do. The stage is shared and the
       * verb is not, so the verb is carried separately rather than by splitting the stage.
       */
      kind,

      /** Set once the API has answered with a row. Null until then, and null forever on a rejection. */
      memoId: null,

      /** The memo's own label once there is one, or the failure sentence. Null while nothing is known. */
      detail: null,

      /** 0 to 1 while the bytes go out, or null when the browser will not say how big they are. */
      uploadFraction: null,
    },
  ]

  machinery.set(id, { stopWatching: null, dismissTimer: null })

  return {
    uploading: (fraction) => patch(id, { uploadFraction: fraction }),
    stored: (memo) => follow(id, memo),
    rejected: (message) => settle(id, PHASES.REJECTED, message),
  }
}

/**
 * The API answered: attach the toast to the row and watch it until it stops changing.
 *
 * The watcher is over the *list*, not over the memo object the response produced, and the
 * difference is what keeps this correct rather than merely working. useMemoList merges a poll's
 * rows field-by-field into the objects it already holds, so the object handed in here is the one
 * that gets written to -- today. Looking the memo up by id each time makes that an
 * implementation detail of that file rather than a contract this one depends on, and it means a
 * memo that leaves the page for a moment and comes back is picked up again instead of being
 * silently frozen at the last status this toast happened to see.
 *
 * @param {number} id
 * @param {object} memo The stored row, as the API returned it.
 */
function follow(id, memo) {
  patch(id, {
    memoId: memo.id,
    phase: statusPhase(memo.status),
    detail: describe(memo),
  })

  if (TERMINAL_STATUSES.has(memo.status)) {
    // A text memo is stored with its transcript already set, so the API can answer `ready`
    // straight away and there is nothing to watch for.
    settle(id, statusPhase(memo.status), describe(memo))

    return
  }

  // watch() outside a component, so it has to be stopped by hand -- which is what `machinery`
  // holds it for. There is no setup scope to bind it to: this module is a singleton and the
  // toast outlives whichever component happened to press the button.
  //
  // **The source is a string, and it has to be.** Returning the row itself was the first
  // version and it never fired once: useMemoList merges a poll's response field-by-field into
  // the object it already holds -- deliberately, so an open detail card follows the same object
  // -- so the row's *identity* is the one thing about it that never changes, and identity is
  // what a non-deep watcher compares. The memo transcribed, the card filled in, and the toast
  // sat on "Waiting for a worker…" until it was dismissed. Reproduced in a browser before the
  // fix; a `deep: true` would also work and would re-run this on every field of every write.
  //
  // The two things joined below are exactly the two things the toast renders, so it re-renders
  // when they change and at no other time. `\u001f` (unit separator) rather than a space, because both halves are
  // free text and a separator has to be something neither can contain.
  const stopWatching = watch(
    () => {
      const row = currentMemo(memo.id)

      return row === null ? null : `${row.status}\u001f${describe(row)}`
    },
    () => {
      const row = currentMemo(memo.id)

      // Gone from the list. Not a state to react to: a memo can leave the page because the
      // date filter moved, and it is still being transcribed. Whatever was last known stays on
      // the toast, and if the row comes back this watcher picks it up again.
      if (row === null) {
        return
      }

      patch(id, { phase: statusPhase(row.status), detail: describe(row) })

      if (TERMINAL_STATUSES.has(row.status)) {
        settle(id, statusPhase(row.status), describe(row))
      }
    },
  )

  machinery.get(id).stopWatching = stopWatching
}

/**
 * How to read the memos currently on the strip. Registered by useMemos; see watchMemosIn.
 *
 * A registered function rather than an `import { useMemos }` at the top of this file, and the
 * reason is a cycle: useMemos imports this module to start a toast, so importing it back would
 * put one of the two module bodies half-built when the other runs. Vite resolves that quietly
 * and the resolution depends on evaluation order -- which, for the module whose body creates
 * the app's only memo list, is not a thing to leave to chance.
 *
 * @type {?() => Array<object>}
 */
let readList = null

/**
 * Tell this module where to look for memos.
 *
 * Called once by useMemos when it builds the fast strip. A registration rather than an import
 * because the dependency only points one way: useMemos knows about toasts, and toasts know
 * about whatever list they were handed. That also makes this testable without a list at all --
 * unregistered, a toast simply never advances past the stage the API reported.
 *
 * @param {() => Array<object>} read Returns the memos currently on the strip.
 */
export function watchMemosIn(read) {
  readList = read
}

function currentMemo(memoId) {
  return readList?.().find((memo) => memo.id === memoId) ?? null
}

/** The API's status as one of this module's phases. Anything unrecognised is still working. */
function statusPhase(status) {
  if (status === 'ready') {
    return PHASES.READY
  }

  if (status === 'failed') {
    return PHASES.FAILED
  }

  return status === 'processing' ? PHASES.PROCESSING : PHASES.QUEUED
}

/**
 * The one line of detail a toast carries about its memo, or null when it has nothing to add.
 *
 * A failed memo gets the reason the worker wrote, which is the whole of the answer to
 * "nothing told me the recording was silent" -- see memoFailure.js. Everything else gets the
 * label the card would show, so the toast that says a memo is ready says which memo.
 *
 * **Null while a voice memo has no text yet, and that is a fix rather than an omission.**
 * memoLabel always answers with something, and for a memo that has nothing at all the
 * something is a *wait*: "Transcribing…". Under a toast whose own title reads "Waiting for a
 * worker…" that is the same fact stated twice in words that do not agree -- observed in a
 * browser, on a real recording, and it reads as the card contradicting itself. A toast with
 * one line is the right answer until there is a second thing to say.
 */
function describe(memo) {
  const failure = failureReason(memo)

  if (failure !== null) {
    return failure
  }

  const named = [memo.title, memo.summary, memo.transcript].some(
    (field) => typeof field === 'string' && field.trim() !== '',
  )

  return named ? memoLabel(memo) : null
}

/**
 * Write to one toast, if it is still on screen.
 *
 * Replacing the array rather than mutating the object, so a toast dismissed while a late
 * progress event is in flight stays dismissed. The array is short -- a handful at the very
 * most -- so rebuilding it is cheaper than making every field of every toast individually
 * reactive.
 */
function patch(id, fields) {
  toasts.value = toasts.value.map((toast) => (toast.id === id ? { ...toast, ...fields } : toast))
}

/**
 * This submission is over: write the last state, stop watching, and decide whether it goes away
 * on its own.
 *
 * **A success dismisses itself and a failure does not,** which is the one asymmetry worth
 * arguing for. A memo that transcribed fine needs no decision from anybody, and a corner that
 * fills up with receipts for things that went right is a corner people stop reading. A failure
 * is the opposite: it carries the only copy of the explanation on the screen, and it is exactly
 * the sentence somebody walking back to their desk needs to still be there. The memo's own card
 * carries the same reason, so nothing is lost when it is dismissed -- but it must be dismissed
 * rather than expiring.
 */
function settle(id, phase, detail) {
  patch(id, { phase, detail })

  const parts = machinery.get(id)

  if (parts === undefined) {
    return
  }

  parts.stopWatching?.()
  parts.stopWatching = null

  if (phase === PHASES.READY) {
    parts.dismissTimer = setTimeout(() => dismiss(id), READY_DISMISS_MS)
  }
}

/**
 * Drop any toast following a memo that no longer exists.
 *
 * **Called by the delete path.** A memo can be deleted while it is still being transcribed
 * -- the detail card's Delete works at any status -- and without this its toast sits at
 * "Transcribing…" for the life of the page. The watcher cannot rescue it: it reads null
 * from the list and correctly declines to react, because a memo vanishing from the *page*
 * is not the same as one that is gone, and a date filter can do the first without the
 * second. Only the caller knows which happened, so only the caller can say.
 *
 * There is a second, smaller effect, stated narrowly because the first version of this note
 * overstated it and the measurement disagreed. A stranded toast in a working phase still
 * asks useProcessingProgress for a bar, which re-registers a clock for the deleted row every
 * time the toast re-renders -- so MemoStrip's `forget` deletes it and the toast puts it back,
 * for as long as anything *else* is still transcribing and driving the tick. It is churn on a
 * row that does not exist rather than a timer that runs forever: counted through a patched
 * setInterval, the ordinary path opens one interval and closes it, ending at zero.
 *
 * @param {string} memoId
 */
export function forgetMemo(memoId) {
  for (const toast of toasts.value.filter((one) => one.memoId === memoId)) {
    dismiss(toast.id)
  }
}

/**
 * Say why a memo was thrown away, and make sure exactly one card says it.
 *
 * Called by the discard path in useMemoList, for a recording the worker found nothing in. That
 * memo is about to stop existing, so this is the *only* place its reason will ever be shown --
 * there will be no card to open and no row to poll. That is the whole reason the discard is
 * driven from the browser rather than from the worker: the deletion and the explanation have to
 * be the same event, and only one of the two runtimes is in front of the user.
 *
 * **Reuses the submission's toast when there is one, rather than adding a second.** In the
 * common case the user recorded the memo seconds ago and a toast is already following it
 * through "Transcribing…"; settling that one turns it into the failure card in place, which is
 * exactly what it would have done had the memo stayed. Starting a fresh toast instead would put
 * two cards in the corner about one recording, one of them stuck mid-wait forever, because the
 * watcher it depends on is about to lose its row.
 *
 * When there is no such toast -- a second tab, a page opened after the fact, a memo recorded
 * before a reload -- it starts one already settled. A memo disappearing with no word at all is
 * the failure mode this whole feature exists to avoid, and it would be the easy one to ship:
 * everything still works, and the user's recording is simply gone.
 *
 * @param {object} memo The memo as the list last saw it, still carrying its reason.
 */
export function reportDiscarded(memo) {
  const detail = describe(memo)
  const following = toasts.value.filter((one) => one.memoId === memo.id)

  if (following.length > 0) {
    for (const toast of following) {
      settle(toast.id, PHASES.FAILED, detail)
    }

    return
  }

  const started = startMemoToast('discard')

  // `stored` rather than reaching into the internals: it is the documented way to point a
  // toast at a row, and a `failed` memo is terminal, so follow() settles it immediately
  // instead of setting up a watcher for a row that is about to be deleted.
  started.stored(memo)
}

/** Take one toast off the screen and forget everything hanging off it. */
export function dismiss(id) {
  const parts = machinery.get(id)

  if (parts !== undefined) {
    parts.stopWatching?.()

    if (parts.dismissTimer !== null) {
      clearTimeout(parts.dismissTimer)
    }

    machinery.delete(id)
  }

  toasts.value = toasts.value.filter((toast) => toast.id !== id)
}

export function useMemoToasts() {
  return { toasts, dismiss, PHASES }
}

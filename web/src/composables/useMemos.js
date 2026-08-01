import { ref } from 'vue'
import {
  acknowledgeReminder,
  createMemo,
  createVoiceMemo,
  deleteReminder,
  createReminder,
  patchMemo,
} from '../api/memos'
import { applyMemoEverywhere, createMemoList } from './useMemoList'

/*
 * The fast strip, and every write that changes a memo.
 *
 * **What changed, and why the shape did.** This file used to hold the list itself -- the
 * array, the debounce, the poll's stop condition -- as module-scoped refs, on the argument
 * that the composer had to prepend into the same array the list rendered without routing
 * props through App.vue. That argument still holds and is why there is still a singleton
 * here. What broke was the assumption underneath it: there is more than one list now, since
 * opening a collection shows its memos alongside the strip, and those two filter
 * independently. So the machinery moved to createMemoList and this file keeps exactly one
 * instance of it.
 *
 * The instance is scoped to `collection: 'none'` for the life of the app, and that is not a
 * default -- it is what the fast strip *is*. A fast memo is one recorded without stopping to
 * file it, which the API spells `collection_id IS NULL`, so a newly created memo always
 * belongs here and nowhere else. That is what keeps the prepend below honest: the row the
 * API just returned is unfiled, so it is genuinely a member of this list.
 *
 * No Pinia. A store would buy devtools time-travel and module namespacing for state that is
 * a handful of refs and one factory call, and it fails the same over-engineering test this
 * project applies to every other dependency. The shape a store would give is already here --
 * state outside the component tree, mutated only by the functions in this file.
 *
 * The microphone is deliberately not part of this. useRecorder owns a MediaRecorder and a
 * live MediaStream, which belong to the one component drawing the button rather than to the
 * app; all this file knows about MEMO-10 is that a Blob can be posted.
 */

/**
 * The one long-lived list: every memo in no collection, newest first.
 *
 * @see createMemoList for everything about how it loads, filters and reconciles.
 */
const fastMemos = createMemoList({ collection: 'none' })

const saving = ref(false)

/**
 * The same thing for the recording path, and separate from `saving` rather than sharing
 * it (MEMO-10).
 *
 * One flag across both was the first shape and it loses memos. Each of these is a
 * re-entry guard -- `store()` refuses to start while its own flag is set -- so a shared
 * one also refuses the *other* action, and the two are not equally cheap to refuse. A
 * refused Save leaves the text in the textarea to try again. A refused upload has
 * nothing to go back to: useRecorder has already assembled the Blob, released the
 * microphone and forgotten the chunks, so the only copy of that recording is the
 * argument being dropped on the floor -- silently, since a guard that returns early sets
 * no error either.
 *
 * Reachable by typing a memo, pressing Save, and pressing Stop while that POST is in
 * flight. Nothing prevented that: the composer stays live during a recording.
 *
 * What the shared flag was bought for -- "the two controls cannot post at once" -- was
 * never needed. Two POSTs in flight make two independent rows, which is what two memos
 * are.
 */
const uploading = ref(false)

/**
 * How much of the recording has gone out, 0 to 1, or null when the browser will not say.
 *
 * Separate from `uploading` rather than folded into it, because they answer different
 * questions and one of them can be unanswerable. `uploading` is the re-entry guard and is
 * never in doubt; this is a measurement that a chunked request body simply does not
 * carry, and null is how the bar is told to show motion without a number rather than to
 * show zero. See api/memos.js, which only calls back on a computable length.
 *
 * Not reset when the upload finishes. `uploading` going false is what takes the bar off
 * the screen, and blanking this as well would make it visibly empty for the frame in
 * between.
 */
const uploadProgress = ref(null)

/**
 * Three error refs, not one, plus the list's own.
 *
 * A failed refresh must not blank the message explaining why the memo the user just typed
 * was rejected, and a rejected memo must not masquerade as a broken list. A rejected
 * recording and a rejected typed memo are different actions with different remedies --
 * record again, or fix the text -- and each message belongs next to the control that
 * produced it.
 *
 * All are prefixed with the operation that failed, which is not decoration: a stopped api
 * container fails every one of these with the same sentence, and the messages then render
 * one under the other with nothing to tell them apart.
 *
 * `audioError` is the *upload* half only. Why the microphone would not open is
 * useRecorder's, and MemoRecorder renders whichever of the two applies.
 */
const saveError = ref(null)
const audioError = ref(null)

/**
 * The fourth: filing a memo, or setting and clearing reminders.
 *
 * One ref across all of those rather than one each, and the reason is where they are shown
 * rather than laziness. Every one of them is an action taken inside the memo detail card,
 * on one memo, one at a time -- so there is exactly one place to put the message and never
 * two of them current at once. That is the same test the three above pass and the opposite
 * conclusion, because those three belong to three controls that are all on screen together.
 */
const memoError = ref(null)

/**
 * POST one text memo and put it at the top of the strip.
 *
 * The row arrives `queued`, which flips `pending` and is what starts the poll. Nothing here
 * talks to the timer. The row it returns is the same shape the list carries
 * (api/app/Services/Memos/Memo.php exists to guarantee that), which is what lets store()
 * prepend it rather than re-running load().
 *
 * Prepending is also what keeps the new memo on screen while a filter is active, and it
 * agrees with what the API would answer rather than working around it: a memo that has not
 * been enriched yet is pinned into every filtered page regardless of match
 * (MemoRepository::list). Once the worker finishes with it, a memo that does not match the
 * filter drops out -- which is the filter working, not the memo being lost.
 *
 * The one case where the prepend now disagrees with the next poll is a *date* filter: the
 * pin is bounded by the window, so a memo recorded today does not survive a poll of a list
 * filtered to yesterday. It appears, and one tick later it is gone. That is the correct
 * answer -- it genuinely is not in that range -- and it is the reason the date filter's
 * caption names the range it is showing.
 *
 * @param {string} text
 * @returns {Promise<boolean>} Whether the memo was stored. The composer clears the
 *   textarea only on true, so a rejected memo is still there to fix and resubmit.
 */
function submit(text) {
  // Trimmed before it goes out, so the string the composer judged against the length
  // cap is the string the API is asked to store. StoreMemoRequest trims again -- that
  // is agreement, not reliance -- and the composer's own guard is what refuses a
  // whitespace-only memo before it ever reaches here.
  return store(() => createMemo(text.trim()), saving, saveError, 'Could not save the memo')
}

/**
 * POST one recording and put it at the top of the strip (MEMO-10).
 *
 * Everything true of submit() is true here, which is the point of both paths being one
 * route: the API answers the same 201 carrying the same object, so the row this prepends is
 * the row the next poll brings back, and MEMO-18 reconciles it by id without knowing which
 * kind of memo it is.
 *
 * The one difference is what the row arrives holding. A voice memo has no transcript yet, so
 * it renders as "No transcript yet." until the worker gets to it -- and it is `queued` like
 * any other, which flips `pending` and starts the poll that will replace it.
 *
 * @param {Blob} blob
 * @param {string} filename
 * @returns {Promise<boolean>}
 */
function submitAudio(blob, filename) {
  // Reset before the request rather than after it, so a second recording never shows
  // the first one's finished bar for the instant before its own first progress event.
  uploadProgress.value = null

  return store(
    () => createVoiceMemo(blob, filename, (fraction) => (uploadProgress.value = fraction)),
    uploading,
    audioError,
    'Could not upload the recording',
  )
}

/**
 * The write path both of the above share: post, prepend, count the write.
 *
 * The guard is a parameter rather than one flag in this file, so each action only ever
 * refuses itself. `uploading` above has the whole argument for why that separation is
 * load-bearing rather than tidy.
 *
 * @param {() => Promise<object>} create Performs the request and returns the stored row.
 * @param {import('vue').Ref<boolean>} guard This action's re-entry flag.
 * @param {import('vue').Ref<?string>} target Where this action's failures are reported.
 * @param {string} failure Prefix, so a stopped api container -- which fails every one of
 *   these with the same sentence -- still says which action it broke.
 * @returns {Promise<boolean>} Whether the memo was stored.
 */
async function store(create, guard, target, failure) {
  if (guard.value) {
    return false
  }

  guard.value = true

  try {
    // Not optimistic -- nothing appears until the database has the row -- so there is no
    // rollback path to get wrong. Prepending rather than re-running load(): the API answers
    // 201 with the stored memo precisely so the client needs no second round trip.
    fastMemos.prepend(await create())

    target.value = null

    return true
  } catch (error) {
    target.value = `${failure} — ${error.message}`

    return false
  } finally {
    guard.value = false
  }
}

/**
 * Whether a memo write is in flight. Guards the detail card's controls against a second
 * click while the first is still going.
 */
const working = ref(false)

/**
 * One memo write: run it, report it, and bring every copy of that memo up to date.
 *
 * Every route this wraps answers with the whole memo, which is the reason one function serves
 * three different actions.
 *
 * **`applyMemoEverywhere`, not `fastMemos.applyUpdate`, and the difference was a bug.** This
 * used to update the strip alone. A memo opened from inside a collection belongs to the
 * collection dialog's list, so setting a reminder on one reached the database and changed
 * nothing on screen -- silently, because the request succeeded, which made pressing the button
 * again the obvious response and two reminders the result. Reproduced in a browser before the
 * fix. Handing the memo to every live list removes the question of which list was on screen.
 *
 * @param {() => Promise<object>} write
 * @param {string} failure
 * @returns {Promise<?object>} The updated memo, or null if the write failed. Callers use the
 *   null to decide whether to close a dialog.
 */
async function writeMemo(write, failure) {
  if (working.value) {
    return null
  }

  working.value = true

  try {
    const updated = await write()

    applyMemoEverywhere(updated)
    memoError.value = null

    return updated
  } catch (error) {
    memoError.value = `${failure} — ${error.message}`

    return null
  } finally {
    working.value = false
  }
}

/**
 * File a memo into a collection, or take it back out with null.
 *
 * Followed by a reload of the strip rather than only an in-place update, because this is the
 * one write that changes *membership*: a filed memo is no longer a fast memo and has to
 * leave the strip, which writing its fields cannot express. The reload is also what pulls in
 * whatever else has changed since -- a memo unfiled from a collection has to appear.
 *
 * The collections grid is reloaded by the caller, not here. It has to be -- a move changes
 * two cards' counts and labels -- but importing useCollections into this file to do it would
 * put a dependency between two singletons for one call site, and MemosView already holds
 * both.
 *
 * @param {object} memo The memo being moved, as the caller is rendering it. Passed rather than
 *   just its id so the open card updates even when the memo belongs to a collection's list
 *   rather than to the strip -- see writeMemo.
 * @param {?string} collectionId
 * @returns {Promise<?object>}
 */
async function moveMemo(memo, collectionId) {
  const updated = await writeMemo(
    () => patchMemo(memo.id, collectionId),
    'Could not move the memo',
  )

  if (updated !== null) {
    fastMemos.load()
  }

  return updated
}

/**
 * Set a reminder on a memo.
 *
 * @param {object} memo The memo being reminded about, as the caller is rendering it.
 * @param {string} remindAt An absolute ISO instant. Both the alarm and the timer produce
 *   one; see ReminderFields for where the conversion happens.
 * @param {?string} note
 */
function addReminder(memo, remindAt, note) {
  return writeMemo(
    () => createReminder(memo.id, remindAt, note),
    'Could not set the reminder',
  )
}

/**
 * Delete a reminder.
 *
 * @param {object} memo The memo the reminder hangs off, as the caller is rendering it.
 * @param {string} reminderId
 */
function dropReminder(memo, reminderId) {
  return writeMemo(
    () => deleteReminder(reminderId),
    'Could not remove the reminder',
  )
}

export function useMemos() {
  return {
    // The fast strip, spread so callers read `memos` rather than `fastMemos.memos`. The
    // list's own surface is unchanged from what this file used to export directly, which is
    // what kept the move to a factory from touching every component.
    ...fastMemos,

    saving,
    uploading,
    uploadProgress,
    saveError,
    audioError,
    memoError,
    working,

    submit,
    submitAudio,
    moveMemo,
    addReminder,
    dropReminder,
  }
}

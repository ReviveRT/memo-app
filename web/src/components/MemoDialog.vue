<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import ReminderFields from './ReminderFields.vue'
import { canRetry, failureReason } from '../memoFailure'
import { memoLabel } from '../memoLabel'
import { useCollections } from '../composables/useCollections'
import { ask } from '../composables/useConfirm'
import { useMemos } from '../composables/useMemos'
import { refreshReminders } from '../composables/useReminders'

/*
 * One memo, opened: the transcription as the main event, with its reminders, when it was
 * made, and which collection it is filed in.
 *
 * A native <dialog> with showModal(), not a positioned div, and that decision buys four
 * things this would otherwise have to build and get right: focus moves into the dialog and is
 * trapped there, Escape closes it, everything behind it is inert to clicks and to the
 * accessibility tree, and it renders in the top layer -- above the fixed backdrop, with no
 * z-index to coordinate against styles.css's "nothing needs a z-index" rule.
 */

const props = defineProps({
  /** The memo to show, or null when nothing is open. */
  memo: { type: Object, default: null },
})

const emit = defineEmits(['close', 'changed'])

/**
 * Mirrors UpdateMemoRequest::MAX_TITLE_LENGTH in the API, the way MemoComposer mirrors the
 * text cap: two runtimes cannot share a constant, so the number is repeated with a note
 * saying where the other copy is. The server stays authoritative -- if these disagree, its
 * 422 lands in the same error slot as everything else this card can fail with.
 *
 * Unlike the composer's cap this one *is* on the input as `maxlength`, and the difference is
 * what the field holds. Truncating a pasted memo would silently throw away what somebody
 * wrote; a title is a label being typed, and 200 characters is far past where anyone stops.
 */
const MAX_TITLE_LENGTH = 200

/**
 * Mirrors UpdateMemoRequest::MAX_TRANSCRIPT_LENGTH, which is itself the same number
 * StoreMemoRequest gives a typed memo.
 *
 * On the textarea as `maxlength`, like the title's and unlike the composer's. The composer
 * leaves its cap off the element because truncating a *pasted* memo would silently throw away
 * what somebody wrote; this field is pre-filled with a transcript that is already inside the
 * limit, so the cap can only be reached by typing past it.
 */
const MAX_TRANSCRIPT_LENGTH = 10_000

const { moveMemo, rename, correct, remove, retry, dropReminder, memoError, working } = useMemos()

const { collections } = useCollections()

const dialogEl = ref(null)

/**
 * Whether the title is being edited, and the text of it.
 *
 * An explicit edit mode rather than a permanently editable field, because the title is also
 * the dialog's heading -- an input sitting where an <h2> belongs makes the card read as a form
 * about a memo instead of as the memo. It also matters that the *displayed* label and the
 * *stored* title are different things: memoLabel falls back to the transcript's first line
 * when there is no title, so a field pre-filled with what the heading shows would silently
 * promote that fallback into a real stored title the first time anybody pressed Save.
 * `draft` is seeded from `memo.title` alone for that reason.
 */
const renaming = ref(false)
const draft = ref('')
const titleEl = ref(null)

/**
 * The same three, for the transcript.
 *
 * Separate state rather than one shared "editing" flag, because the two fields are independent
 * edits on one card and closing the title should not discard a half-typed correction. They are
 * both reset when the dialog is pointed at a different memo, which is the case that matters.
 */
const correcting = ref(false)
const transcriptDraft = ref('')
const transcriptEl = ref(null)

/** The collection chosen in the select, which is not applied until Move is pressed. */
const chosenCollection = ref('')

/**
 * Open and close the real dialog when the prop changes.
 *
 * showModal() rather than the `open` attribute, and they are not equivalent: `open` shows a
 * non-modal dialog with no focus trapping, no Escape handling, no backdrop and no top layer.
 * Everything this component picked <dialog> for comes from showModal specifically.
 *
 * Guarded on `dialogEl.value.open`, because calling showModal() on an already-open dialog
 * throws InvalidStateError -- reachable whenever the parent re-renders with a different memo
 * while one is open, which is exactly what clicking a second card does.
 */
watch(
  () => props.memo,
  (memo) => {
    // Reset every control whenever the dialog is pointed at a different memo. Without this,
    // clicking a second card while the first is being renamed leaves the previous memo's draft
    // in the field, over the new memo's heading, one keystroke from being saved onto it. The
    // transcript field is the same hazard with more to lose, since it would be a whole other
    // memo's words about to be written over these ones.
    renaming.value = false
    correcting.value = false
    chosenCollection.value = memo?.collection_id ?? ''

    const el = dialogEl.value

    if (!el) {
      return
    }

    if (memo && !el.open) {
      el.showModal()
    } else if (!memo && el.open) {
      el.close()
    }
  },
  // flush: 'post' so the element exists: on the first open the <dialog> is rendered by the
  // same tick that sets the memo, and without this the watcher runs before the ref is filled.
  { flush: 'post' },
)

/** Reminders still owed, soonest first, and the ones that have already fired. */
const upcoming = computed(
  () => props.memo?.reminders?.filter((reminder) => reminder.delivered_at === null) ?? [],
)

const past = computed(
  () => props.memo?.reminders?.filter((reminder) => reminder.delivered_at !== null) ?? [],
)

/**
 * The collection this memo is in, if any.
 *
 * Resolved against the grid's collections rather than carried on the memo, because the memo
 * only has a `collection_id` -- the name lives on the collection. They are both loaded by the
 * time a memo can be opened, and a miss is rendered as "unknown" rather than as a crash:
 * reachable for a second if another tab deletes the collection between the grid loading and
 * this opening.
 */
const filedIn = computed(() =>
  props.memo?.collection_id === null || props.memo?.collection_id === undefined
    ? null
    : (collections.value.find((one) => one.id === props.memo.collection_id) ?? null),
)

function longDate(iso) {
  const at = new Date(iso)

  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString()
}

/**
 * Whether the select is pointing somewhere other than where the memo already is.
 *
 * What the Move button is enabled by, and the reason the select alone was not enough of a
 * control. It used to apply on `@change`, which made it a switch that quietly rewrote the memo
 * as a side effect of looking through the list -- keyboard users move through a `<select>`'s
 * options with the arrow keys, so *every option passed over* was a write. There was also
 * nothing on the screen that said what it did or that it had done it.
 */
const canMove = computed(
  () => props.memo !== null && chosenCollection.value !== (props.memo.collection_id ?? ''),
)

/** What the Move button will do, said out loud, so the button is not a mystery. */
const moveLabel = computed(() => {
  if (!canMove.value) {
    return filedIn.value === null ? 'Not in a collection' : `In ${filedIn.value.name}`
  }

  if (chosenCollection.value === '') {
    return 'Take out of the collection'
  }

  const target = collections.value.find((one) => one.id === chosenCollection.value)

  return target ? `Move to ${target.name}` : 'Move'
})

/**
 * File this memo somewhere, or unfile it -- on an explicit press.
 *
 * The select's empty value is the fast strip, which is why the value is normalised to null
 * rather than passed through: `<option value="">` yields '' and the API wants an explicit
 * null to mean "take it out of its collection".
 */
async function move() {
  if (!canMove.value) {
    return
  }

  const updated = await moveMemo(props.memo, chosenCollection.value === '' ? null : chosenCollection.value)

  if (updated !== null) {
    // The grid's counts and the strip's membership both changed. The parent owns both, so it
    // is told rather than either being reached into from here.
    emit('changed')
  }
}

/**
 * Escape: cancel the rename if one is open, otherwise close the card.
 *
 * **One keypress was doing both.** The rename field had `@keydown.esc="renaming = false"`,
 * which cancelled the edit -- and then the keypress went on to be the browser's close request
 * for the <dialog>, so the whole memo card shut as well. Reproduced in a browser: one Escape,
 * field gone *and* dialog gone, when the only thing being escaped from was the field.
 *
 * Handled on the dialog's own `cancel` event rather than as a keydown on the input, and that
 * is the difference between fixing it here and fixing it in one place where it can be pressed.
 * `cancel` is what the close request fires, it is cancelable, and it arrives no matter which
 * control inside the dialog has focus -- so Escape from the Save button does the same thing as
 * Escape from the field. A keydown handler on the input would have covered one of the three.
 *
 * When nothing is being renamed this does nothing and the dialog closes, which is the
 * behaviour the component was built with: there is nothing else in here to lose.
 */
function onCancelRequest(event) {
  if (!renaming.value && !correcting.value) {
    return
  }

  event.preventDefault()
  renaming.value = false
  correcting.value = false
}

/** Start renaming, with whatever title is stored -- not the fallback the heading is showing. */
async function startRename() {
  draft.value = props.memo?.title ?? ''
  renaming.value = true

  // After the field exists. `autofocus` only applies on page load and does nothing for an
  // element revealed by a click.
  await nextTick()
  titleEl.value?.select()
}

/**
 * Save the new title. An empty field clears it rather than being refused.
 *
 * Clearing is a real operation: the title is generated, and an owner who disagrees with the
 * guess may want the memo to fall back to its own first line rather than to a different guess.
 * The API takes null for exactly that, and this is where '' becomes null.
 */
async function saveRename() {
  const next = draft.value.trim()

  if ((await rename(props.memo, next === '' ? null : next)) !== null) {
    renaming.value = false
  }

  // Left open on failure, with the message underneath, so the text is still there to retry.
}

/** Start correcting, from the stored transcript. */
async function startCorrecting() {
  transcriptDraft.value = props.memo?.transcript ?? ''
  correcting.value = true

  // Focus without selecting, unlike startRename. A title is short and usually replaced
  // wholesale, so selecting it saves a step; a transcript is long and usually being corrected
  // in one place, and selecting all of it puts the next keystroke one slip away from wiping it.
  await nextTick()
  transcriptEl.value?.focus()
}

/**
 * Save the corrected transcript.
 *
 * No empty case, unlike saveRename: the button is disabled on a blank field and the API refuses
 * one, because clearing a transcript is not an operation this control should offer. Somebody
 * who wants a memo with no text wants Delete.
 */
async function saveTranscript() {
  const next = transcriptDraft.value.trim()

  if (next === '') {
    return
  }

  if ((await correct(props.memo, next)) !== null) {
    correcting.value = false
  }

  // Left open on failure, with the message underneath, so the typing is still there to retry.
}

/**
 * Delete, after asking.
 *
 * **The wording names what else goes, which is the reason this is not a bare "Are you sure?".**
 * A memo is not only its transcript -- deleting one takes the recording off the volume and
 * cascades its reminders -- and none of that is visible from the card. Left to guess, a careful
 * person assumes the worst of the possibilities and does not press it.
 *
 * The question and the consequence are two arguments rather than one sentence, so the dialog
 * can set them as a heading and a line under it. That is one of the things `window.confirm`
 * could not do, and it is why this now goes through useConfirm.
 */
async function confirmDelete() {
  const alarms = upcoming.value.length + past.value.length
  const extra = [
    props.memo?.source === 'voice' ? 'the recording' : null,
    alarms === 1 ? '1 reminder' : alarms > 1 ? `${alarms} reminders` : null,
  ].filter(Boolean)

  const agreed = await ask({
    title: `Delete “${memoLabel(props.memo)}”?`,
    body: [
      extra.length ? `This also removes ${extra.join(' and ')}.` : null,
      'It cannot be undone.',
    ]
      .filter(Boolean)
      .join(' '),
    confirmLabel: 'Delete memo',
    danger: true,
  })

  if (!agreed) {
    return
  }

  if ((await remove(props.memo)) !== null) {
    // Closed first: the dialog is rendering a memo that no longer exists, and `changed` makes
    // the parent reload the collections grid, whose counts have just dropped by one.
    emit('close')
    emit('changed')
  }
}

/**
 * Drop one reminder.
 *
 * Named for what it removes rather than just `remove`, which is now taken by the memo delete
 * imported above -- and which would be a confusing pair of names on one component even if the
 * compiler allowed it.
 */
async function removeReminder(reminderId) {
  if ((await dropReminder(props.memo, reminderId)) !== null) {
    // So the delivery loop stops counting on a reminder that no longer exists, rather than
    // finding out up to a minute later.
    refreshReminders()

    emit('changed')
  }
}
</script>

<template>
  <!--
    @close is the browser's own event, fired by Escape and by close(). Listening to it rather
    than only to the button is what keeps the parent's state in step when the dialog is
    dismissed by a route the parent did not initiate.

    @cancel is handled rather than left alone, but only conditionally -- Escape still closes
    this card, except while the title is being renamed, where it belongs to the edit that is
    open. See onCancelRequest for the bug that came of not doing it here. Nothing else in the
    dialog would be lost to a close: the reminder form is the only other input, and re-opening
    the memo gets back to it in one click.

    The backdrop click is handled by comparing the event target to the dialog itself, which is
    the standard trick: clicks on ::backdrop report the <dialog> as their target, while clicks
    on anything inside report that child.
  -->
  <dialog
    ref="dialogEl"
    class="sheet"
    @cancel="onCancelRequest"
    @close="emit('close')"
    @click="$event.target === dialogEl && emit('close')"
  >
    <article v-if="memo" class="sheet__body">
      <header class="sheet__head">
        <div class="sheet__heading">
          <!--
            The title is the heading, and the heading is editable — but only once asked. See
            `renaming` for why the field is not simply always there, and why its initial value
            is `memo.title` rather than the heading's own text.
          -->
          <form v-if="renaming" class="sheet__rename" @submit.prevent="saveRename">
            <label class="sheet__rename-field">
              <span class="sr-only">Memo title</span>
              <input
                ref="titleEl"
                v-model="draft"
                type="text"
                :maxlength="MAX_TITLE_LENGTH"
                placeholder="Give this memo a name…"
                :disabled="working"
              />
            </label>

            <button type="submit" :disabled="working">Save</button>
            <button type="button" class="ghost" :disabled="working" @click="renaming = false">
              Cancel
            </button>
          </form>

          <template v-else>
            <h2 class="sheet__title">{{ memoLabel(memo) }}</h2>

            <!--
              Beside the title rather than in a menu, because a generated title is wrong often
              enough that renaming is an ordinary thing to do rather than an advanced one.
              aria-label carries the memo's name so a screen reader hears which memo this
              renames when it is reached out of context.
            -->
            <button
              type="button"
              class="ghost sheet__rename-open"
              :disabled="working"
              :aria-label="`Rename ${memoLabel(memo)}`"
              @click="startRename"
            >
              Rename
            </button>
          </template>

          <p class="sheet__meta">
            <span class="badge" :class="`badge--${memo.status}`">{{ memo.status }}</span>
            <span class="badge badge--source">{{ memo.source }}</span>
            <time :datetime="memo.created_at">{{ longDate(memo.created_at) }}</time>
          </p>
        </div>

        <button type="button" class="sheet__close" aria-label="Close" @click="emit('close')">
          ×
        </button>
      </header>

      <!--
        The transcription, as the main part of the card. First in the body and given the most
        room, because it is what the memo *is* -- everything else here is metadata about it.
      -->
      <section class="sheet__section">
        <h3 class="sheet__label">Transcription</h3>

        <!--
          Editing, when asked for. The same explicit mode the title uses rather than a
          permanently live textarea, and for a stronger reason here: this is the memo itself,
          and a card whose main content is a form field reads as something half-written rather
          than as something recorded.
        -->
        <template v-if="correcting">
          <textarea
            ref="transcriptEl"
            v-model="transcriptDraft"
            class="sheet__transcript-input"
            rows="6"
            :maxlength="MAX_TRANSCRIPT_LENGTH"
            aria-label="Transcription"
            @keydown.enter.meta.prevent="saveTranscript"
            @keydown.enter.ctrl.prevent="saveTranscript"
          ></textarea>

          <div class="sheet__transcript-actions">
            <!--
              Disabled on empty, matching the API rather than discovering it: a blank
              transcript is a 422, because a memo with no text is unfindable by search and
              indistinguishable from one whose recording produced nothing. Clearing a *title*
              is allowed, which is why that button has no such guard.
            -->
            <button type="button" :disabled="working || transcriptDraft.trim() === ''" @click="saveTranscript">
              Save transcription
            </button>

            <button type="button" class="ghost" :disabled="working" @click="correcting = false">
              Cancel
            </button>
          </div>
        </template>

        <template v-else>
          <!--
            white-space: pre-wrap in the stylesheet, so the newlines somebody typed into the
            textarea survive. Interpolation escapes the text, so a memo containing markup is
            shown, not run.
          -->
          <p v-if="memo.transcript" class="sheet__transcript">{{ memo.transcript }}</p>

          <p v-else-if="failureReason(memo)" class="sheet__transcript sheet__transcript--failed">
            {{ failureReason(memo) }}
          </p>

          <p v-else class="sheet__transcript sheet__transcript--empty">
            Still being transcribed — this card will fill in on its own.
          </p>

          <!--
            Offered only once there is something to correct. A memo still being transcribed has
            no text to edit, and one that failed has no text at all -- Retry is that memo's
            control, and an Edit button beside it would invite typing a transcript for a
            recording nobody has heard.

            A `.ghost` like Rename, because this is an edit to a memo that is fine. The filled
            button on this card belongs to Retry, which is the offered fix for one that is not.
          -->
          <button v-if="memo.transcript" type="button" class="ghost" @click="startCorrecting">
            Edit transcription
          </button>
        </template>
      </section>

      <!--
        A memo that failed *after* producing text -- a retry that got a transcript and then hit
        something else, which MEMO-16's retry path makes reachable. The reason goes under the
        transcript rather than replacing it, because both are true and the transcript is the
        part worth keeping.
      -->
      <p
        v-if="memo.transcript && failureReason(memo)"
        class="notice notice--error"
        role="status"
      >
        {{ failureReason(memo) }}
      </p>

      <!--
        And the way out of it (MEMO-17). Directly under whichever of the two above showed the
        reason, because the reason is what somebody acts on and the action should not be
        somewhere else on the card.

        The hint is not filler: most of these sentences describe something the reader can fix,
        and the worker's own retries are long over by the time they have read one -- three
        attempts inside a couple of minutes of the recording. Without saying so, "Retry" looks
        like the thing that has already been tried and failed.

        A filled button rather than a `.ghost`, unlike Rename and Delete beside it. Those are
        edits to a memo that is fine; this is the offered fix for a memo that is not, and it is
        the only thing on this card the user is being invited to do.
      -->
      <div v-if="canRetry(memo)" class="sheet__retry">
        <button type="button" @click="retry(memo)">Try transcribing again</button>

        <p class="sheet__hint">
          The worker gave up on this one. If you have changed something since — a setting, a key
          — this puts it back at the front of the queue.
        </p>
      </div>

      <section v-if="memo.tags?.length" class="sheet__section">
        <h3 class="sheet__label">Tags</h3>

        <!--
          Keyed by position, not by the tag itself. Nothing guarantees these are unique --
          `tags` is a plain text[] with no constraint and MEMO-21 fills it from model output --
          and a repeated tag would key two nodes the same, which Vue reports as "Duplicate keys
          found during update".
        -->
        <ul class="tags">
          <li v-for="(tag, at) in memo.tags" :key="at" class="tags__tag">{{ tag }}</li>
        </ul>
      </section>

      <section class="sheet__section">
        <h3 class="sheet__label">Collection</h3>

        <!--
          A select *and* a button, where there used to be a select that applied on change.

          The old control was the whole of "move this memo" and did not look like anything: no
          verb, no confirmation, and no way to look through the list without committing to each
          option on the way past — arrow-keying through a <select> fires `change` on every one,
          so a keyboard user filed the memo into four collections in a row to reach the fifth.
          It read as decorative because nothing about it said it was a control that did
          something, and then it did something without being asked.

          Now choosing is inert and the button is the action, with the destination in its label
          so it says what will happen before it happens. Disabled when the choice is where the
          memo already is, where it would be a no-op — and it then shows where that is, so the
          disabled state answers a question rather than just refusing.

          Still a <select> rather than a list of buttons: it is a single choice from a set that
          grows, and it is the control that already handles a long list on every platform
          including a phone.
        -->
        <div class="sheet__move">
          <label class="sheet__field">
            <span class="sr-only">Choose a collection for this memo</span>

            <select v-model="chosenCollection" class="sheet__select" :disabled="working">
              <option value="">Fast memos (no collection)</option>

              <option v-for="one in collections" :key="one.id" :value="one.id">
                {{ one.name }}
              </option>
            </select>
          </label>

          <button type="button" :disabled="working || !canMove" @click="move">
            {{ moveLabel }}
          </button>
        </div>

        <p v-if="memo.collection_id && !filedIn" class="sheet__hint">
          Filed in a collection this page has not loaded.
        </p>
      </section>

      <section class="sheet__section">
        <h3 class="sheet__label">Reminders</h3>

        <ul v-if="upcoming.length" class="reminders">
          <li v-for="reminder in upcoming" :key="reminder.id" class="reminders__row">
            <div>
              <time :datetime="reminder.remind_at" class="reminders__when">
                {{ longDate(reminder.remind_at) }}
              </time>

              <p v-if="reminder.note" class="reminders__note">{{ reminder.note }}</p>
            </div>

            <button
              type="button"
              class="reminders__drop"
              :disabled="working"
              @click="removeReminder(reminder.id)"
            >
              Remove
            </button>
          </li>
        </ul>

        <p v-else class="sheet__hint">Nothing set. Add an alarm or a timer below.</p>

        <!--
          Both of the brief's controls -- an alarm at a wall-clock time, and a timer some
          minutes out -- live in here. They are two ways of choosing one instant, which is why
          they are one component and one API field.
        -->
        <ReminderFields :memo="memo" @changed="emit('changed')" />

        <!--
          The ones that have already fired, kept visible rather than hidden. A reminder that
          went off is the answer to "did it remind me?", and removing it from the card the
          moment it fires makes that unanswerable.
        -->
        <details v-if="past.length" class="sheet__past">
          <summary>{{ past.length }} already sent</summary>

          <ul class="reminders">
            <li v-for="reminder in past" :key="reminder.id" class="reminders__row">
              <div>
                <time :datetime="reminder.remind_at" class="reminders__when">
                  {{ longDate(reminder.remind_at) }}
                </time>

                <p v-if="reminder.note" class="reminders__note">{{ reminder.note }}</p>
              </div>

              <button
                type="button"
                class="reminders__drop"
                :disabled="working"
                @click="removeReminder(reminder.id)"
              >
                Remove
              </button>
            </li>
          </ul>
        </details>
      </section>

      <!--
        Last, and on its own row, because it is the one thing on this card that cannot be
        undone. Everything above it is a change; this ends the memo. Putting it beside Rename
        in the header would make the two look like a pair of equally ordinary edits, and it is
        two pixels from the control somebody reaches for most.

        A ghost button rather than a filled red one: it should be findable and not inviting.
        The confirmation names what else goes with it — see confirmDelete.
      -->
      <footer class="sheet__foot">
        <button
          type="button"
          class="ghost ghost--danger"
          :disabled="working"
          @click="confirmDelete"
        >
          Delete memo
        </button>
      </footer>

      <!--
        One error slot for every write this card can make. useMemos has the argument for why
        those share a ref where the composer's and the recorder's do not: these are all
        actions on one memo, taken one at a time, in one place.
      -->
      <p v-if="memoError" class="notice notice--error" role="alert">{{ memoError }}</p>
    </article>
  </dialog>
</template>

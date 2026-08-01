<script setup>
import { computed, ref, watch } from 'vue'
import ReminderFields from './ReminderFields.vue'
import { memoLabel } from '../memoLabel'
import { useCollections } from '../composables/useCollections'
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

const { moveMemo, dropReminder, memoError, working } = useMemos()
const { collections } = useCollections()

const dialogEl = ref(null)

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
 * File this memo somewhere, or unfile it.
 *
 * The select's empty value is the fast strip, which is why the value is normalised to null
 * rather than passed through: `<option value="">` yields '' and the API wants an explicit
 * null to mean "take it out of its collection".
 */
async function move(value) {
  const updated = await moveMemo(props.memo, value === '' ? null : value)

  if (updated !== null) {
    // The grid's counts and the strip's membership both changed. The parent owns both, so it
    // is told rather than either being reached into from here.
    emit('changed')
  }
}

async function remove(reminderId) {
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

    @cancel.prevent is deliberately *not* used: Escape should close this. There is nothing
    here that would be lost -- the reminder form is the only input, and re-opening the memo
    gets back to it in one click.

    The backdrop click is handled by comparing the event target to the dialog itself, which is
    the standard trick: clicks on ::backdrop report the <dialog> as their target, while clicks
    on anything inside report that child.
  -->
  <dialog ref="dialogEl" class="sheet" @close="emit('close')" @click="$event.target === dialogEl && emit('close')">
    <article v-if="memo" class="sheet__body">
      <header class="sheet__head">
        <div>
          <h2 class="sheet__title">{{ memoLabel(memo) }}</h2>

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
          white-space: pre-wrap in the stylesheet, so the newlines somebody typed into the
          textarea survive. Interpolation escapes the text, so a memo containing markup is
          shown, not run.
        -->
        <p v-if="memo.transcript" class="sheet__transcript">{{ memo.transcript }}</p>

        <p v-else class="sheet__transcript sheet__transcript--empty">
          {{
            memo.status === 'failed'
              ? 'This recording could not be transcribed.'
              : 'Still being transcribed — this card will fill in on its own.'
          }}
        </p>
      </section>

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
          A <select> rather than a list of buttons, because this is a single choice among a
          set that grows, and it is the control that already handles a long list on every
          platform including a phone.
        -->
        <label class="sheet__field">
          <span class="sr-only">Move this memo to a collection</span>

          <select
            class="sheet__select"
            :value="memo.collection_id ?? ''"
            :disabled="working"
            @change="move($event.target.value)"
          >
            <option value="">Fast memos (no collection)</option>

            <option v-for="one in collections" :key="one.id" :value="one.id">
              {{ one.name }}
            </option>
          </select>
        </label>

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
              @click="remove(reminder.id)"
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
                @click="remove(reminder.id)"
              >
                Remove
              </button>
            </li>
          </ul>
        </details>
      </section>

      <!--
        One error slot for every write this card can make. useMemos has the argument for why
        those share a ref where the composer's and the recorder's do not: these are all
        actions on one memo, taken one at a time, in one place.
      -->
      <p v-if="memoError" class="notice notice--error" role="alert">{{ memoError }}</p>
    </article>
  </dialog>
</template>

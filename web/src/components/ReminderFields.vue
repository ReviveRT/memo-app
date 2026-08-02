<script setup>
import { ref } from 'vue'
import { useMemos } from '../composables/useMemos'
import { askToNotify, refreshReminders, useReminders } from '../composables/useReminders'

/*
 * The two ways to set a reminder: an alarm at a wall-clock time, and a timer some minutes
 * out.
 *
 * **They produce the same thing.** Both resolve to one absolute instant before the request is
 * made, so the API has one field, one column and no idea which control was used -- see
 * StoreReminderRequest. That is what keeps "in 30 minutes" from needing the server to know
 * when "now" was, and what keeps the alarm from needing it to know the user's timezone.
 *
 * The conversion is the whole of this component's logic, and the alarm half is the part with
 * a trap in it: `<input type="datetime-local">` yields `2026-08-02T09:00`, with no offset, and
 * `new Date(...)` parses that as *local* time -- which is what the user meant. `Date.parse` of
 * the same string is specified to treat a date-time with no offset as local too, so both agree
 * here; what would break it is appending a 'Z' to "make it ISO", which would silently shift
 * the alarm by the reader's UTC offset. It is left alone and converted with toISOString().
 */

const props = defineProps({
  memo: { type: Object, required: true },
})

const emit = defineEmits(['changed'])

const { addReminder, working } = useMemos()
const { canAskToNotify, canNotify, permission } = useReminders()

/**
 * The quick timers, in minutes.
 *
 * Five is the shortest offered, deliberately: the API refuses a time in the past and allows
 * only a minute of clock skew, so a "one minute" button would be the one most likely to be
 * refused for reasons the user cannot see. Five is also about the shortest interval for which
 * a reminder is worth setting rather than just remembering.
 */
const TIMERS = [
  { minutes: 5, label: '5 min' },
  { minutes: 15, label: '15 min' },
  { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 hour' },
  { minutes: 180, label: '3 hours' },
  { minutes: 1440, label: 'Tomorrow' },
]

const alarmAt = ref('')
const note = ref('')

/**
 * The earliest the alarm picker will accept: one minute from now, local, formatted the way
 * `datetime-local` wants it.
 *
 * The browser greying out past times is a better answer than the API's 422, which is the
 * backstop rather than the interface.
 *
 * **A plain function, not a computed, and that is a correction.** It was a `computed`, with a
 * comment claiming it was recomputed rather than cached -- which is exactly backwards: a
 * computed caches until one of its reactive dependencies changes, and `Date.now()` is not one,
 * so it would have been evaluated once and frozen for the life of the component. Called from
 * the template it is evaluated on each render instead, and since the card is torn down and
 * rebuilt whenever a different memo is opened, the floor is never older than the card.
 *
 * It can still go stale on a card left open across the minute boundary. That is harmless in
 * the direction that matters -- the floor is a minute in the past at worst, which the API's own
 * PAST_TOLERANCE_SECONDS already allows for.
 */
function earliest() {
  const at = new Date(Date.now() + 60_000)

  return [
    at.getFullYear(),
    '-',
    String(at.getMonth() + 1).padStart(2, '0'),
    '-',
    String(at.getDate()).padStart(2, '0'),
    'T',
    String(at.getHours()).padStart(2, '0'),
    ':',
    String(at.getMinutes()).padStart(2, '0'),
  ].join('')
}

/**
 * Set a reminder, having first asked about notifications if it is still worth asking.
 *
 * **The permission prompt is here and nowhere else.** Asking on page load is the pattern
 * browsers penalise -- no user gesture behind it, so Chrome and Firefox suppress or auto-deny
 * -- and a denial is permanent, which makes a badly timed ask worse than no ask. The moment
 * somebody sets a reminder is the moment the request explains itself.
 *
 * The result is not checked, and that is deliberate: a refused permission does not refuse the
 * reminder. It still fires, as an in-app banner, which is what useReminders guarantees on its
 * own. The prompt is an upgrade, not a precondition.
 */
async function set(instant) {
  if (canAskToNotify.value) {
    await askToNotify()
  }

  const created = await addReminder(
    props.memo,
    instant,
    note.value.trim() === '' ? null : note.value.trim(),
  )

  if (created === null) {
    return
  }

  alarmAt.value = ''
  note.value = ''

  // So the delivery loop schedules against it now rather than on its next minute tick. The
  // shortest timer offered is five minutes, so a whole minute of unawareness would be a fifth
  // of the wait.
  refreshReminders()

  emit('changed')
}

/** A relative timer: minutes from this moment, resolved against the browser's clock. */
function setTimer(minutes) {
  return set(new Date(Date.now() + minutes * 60_000).toISOString())
}

/**
 * An alarm: the local date and time the user picked.
 *
 * `new Date(value)` on a `datetime-local` value is local-time parsing, which is the reading
 * intended -- see the header for the 'Z' trap this avoids. toISOString() then converts to the
 * instant the API stores.
 */
function setAlarm() {
  if (alarmAt.value === '') {
    return
  }

  const at = new Date(alarmAt.value)

  if (Number.isNaN(at.getTime())) {
    return
  }

  return set(at.toISOString())
}
</script>

<template>
  <div class="remind">
    <!--
      The note is shared by both controls rather than duplicated into each, because it is the
      "about something" half of the brief's request and it means the same thing whichever way
      the time was chosen. Above both, so it is filled in before the button that commits.
    -->
    <label class="remind__note">
      <span class="sr-only">What to be reminded about</span>
      <input
        v-model="note"
        type="text"
        maxlength="200"
        placeholder="What about? (optional)"
        :disabled="working"
      />
    </label>

    <div class="remind__timers" role="group" aria-label="Set a timer">
      <button
        v-for="timer in TIMERS"
        :key="timer.minutes"
        type="button"
        class="chip"
        :disabled="working"
        @click="setTimer(timer.minutes)"
      >
        {{ timer.label }}
      </button>
    </div>

    <!--
      submit.prevent so Enter in the picker sets the alarm, which is what a single-field form
      inside a dialog should do -- and so it does not submit the page.
    -->
    <form class="remind__alarm" @submit.prevent="setAlarm">
      <label class="remind__field">
        <span class="sr-only">Reminder date and time</span>
        <input v-model="alarmAt" type="datetime-local" :min="earliest()" :disabled="working" />
      </label>

      <button type="submit" :disabled="working || alarmAt === ''">Set reminder</button>
    </form>

    <!--
      Said once, quietly, and only when it is true. The app cannot promise a notification it
      is not allowed to show, and it should not pretend the reminder is lost either -- the
      in-app banner always fires. Both halves of that are stated rather than one.
    -->
    <p v-if="permission === 'denied'" class="remind__hint">
      Notifications are blocked for this site, so reminders will appear here in the app
      instead.
    </p>

    <p v-else-if="!canNotify" class="remind__hint">
      Reminders appear in the app. Setting one will ask whether to show system notifications
      too.
    </p>

    <!--
      The honest limit, and the reason it is on screen rather than only in a comment: somebody
      who sets an alarm for 7am and closes the laptop will otherwise reasonably expect it to go
      off. There is no service worker and no push, so it cannot.
    -->
    <p class="remind__hint">Reminders fire while this app is open in a tab.</p>
  </div>
</template>

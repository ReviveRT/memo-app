<script setup>
import { useReminders } from '../composables/useReminders'

/*
 * Reminders that have fired, on screen until they are dismissed.
 *
 * **This is the delivery channel, not a fallback for one.** A system notification is the
 * upgrade -- it reaches somebody in another window -- but it is the part that can be refused,
 * suppressed by the browser, or unavailable entirely. This always fires, which is what lets
 * useReminders promise anything at all about a reminder coming due while the app is open.
 *
 * It is also the *whole* of the catch-up path: reminders that came due while the app was
 * closed land here without a system notification, because eleven of those at once after a
 * weekend would be a notification storm rather than a reminder. See useReminders for the
 * split.
 *
 * Fixed to the corner rather than in the document flow, so a reminder firing does not reflow
 * the page under the cursor -- and so it is visible whether the reader is at the top of the
 * memo strip or the bottom of the collections grid.
 */

const { delivered, dismiss, dismissAll } = useReminders()

function at(iso) {
  const when = new Date(iso)

  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString()
}
</script>

<template>
  <!--
    role="status" with aria-live="polite", not role="alert". A reminder is something the user
    asked for at a time they chose, so it should be announced when the screen reader reaches a
    natural break rather than interrupting mid-sentence. The distinction matters here more than
    usual: this can fire while somebody is reading a transcript.

    The region is always in the DOM and only its contents change, which is the arrangement live
    regions are actually announced from -- a region inserted along with its text is the less
    dependable of the two, and this one is inserted by a timer with no user action behind it.
  -->
  <div class="toasts" role="status" aria-live="polite">
    <div v-for="reminder in delivered" :key="reminder.id" class="toast">
      <div class="toast__body">
        <p class="toast__title">{{ reminder.memo_label }}</p>
        <p v-if="reminder.note" class="toast__note">{{ reminder.note }}</p>
        <time class="toast__when" :datetime="reminder.remind_at">{{ at(reminder.remind_at) }}</time>
      </div>

      <button type="button" class="toast__close" aria-label="Dismiss reminder" @click="dismiss(reminder.id)">
        ×
      </button>
    </div>

    <!--
      Only once there are enough for dismissing them one at a time to be a chore, which is the
      catch-up case this exists for. Two is not a chore.
    -->
    <button v-if="delivered.length > 2" type="button" class="toast__all" @click="dismissAll">
      Dismiss all {{ delivered.length }}
    </button>
  </div>
</template>

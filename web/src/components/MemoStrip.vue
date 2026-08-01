<script setup>
import { computed, watch } from 'vue'
import ProgressBar from './ProgressBar.vue'
import { memoLabel } from '../memoLabel'
import { useProcessingProgress } from '../composables/useProcessingProgress'

/*
 * A single row of memo cards that scrolls sideways.
 *
 * The brief is specific about the shape: one row the width of the screen, scrolling left and
 * right, each card showing a brief label so a memo can be found at a glance. That is a
 * horizontally scrolling grid rather than a wrapping one, and the difference matters -- a
 * wrapping grid grows downward and pushes the collections below it off the screen, which is
 * the thing this layout exists to avoid.
 *
 * Used twice: for the fast strip, and for the memos inside an opened collection. They are the
 * same object in the same arrangement, so they are the same component.
 */

const props = defineProps({
  memos: { type: Array, required: true },

  /**
   * Whether a fresher list is coming. The difference between "empty because nothing has
   * arrived yet" and "empty because that is the answer". Neither this strip nor the
   * collections grid may say "no memos" on the strength of a request that has not happened.
   */
  loading: { type: Boolean, default: false },

  /** Whether the last load failed, so the empty state does not make a claim it cannot support. */
  failed: { type: Boolean, default: false },

  /** The filter the rows came back for, or null. Named in the empty state so a typo is visible. */
  query: { type: String, default: null },

  /** A description of the active date window, for the same reason. */
  dateLabel: { type: String, default: null },

  emptyHint: { type: String, default: 'No memos yet.' },
})

const emit = defineEmits(['open'])

/*
 * The transcribing bar, carried over from the list this strip replaced.
 *
 * It is not decoration and it is not new: MEMO-18's progress work put it on every row that
 * was still being worked on, and a card layout that quietly dropped it would be a regression
 * dressed as a redesign. What it says is the same thing it said there -- the words come from
 * the status, which the server reported, and the bar itself is an estimate carrying no number
 * (see useProcessingProgress for why it cannot be one).
 */
const { progressFor, forget } = useProcessingProgress()

/**
 * The same rule the poll applies: anything not terminal is still working.
 *
 * Repeated here rather than imported from useMemoList, and deliberately -- that module's
 * TERMINAL_STATUSES is private and answers a *behavioural* question (keep polling?), while
 * this is a *presentational* one (draw a bar?). They agree today and should: a row with no bar
 * on a page that is still polling for it would be the page contradicting itself.
 */
const working = (memo) => memo.status !== 'ready' && memo.status !== 'failed'

const waitingIds = computed(() => new Set(props.memos.filter(working).map((memo) => memo.id)))

/**
 * Drop the clock for anything that has finished or fallen off the strip.
 *
 * `flush: 'post'` so this runs after the cards have rendered against the new list. Cleaning up
 * first would delete the entry for a memo whose bar is about to be drawn one last time, and
 * progressFor() would restart its clock at zero -- a bar visibly jumping backwards on the tick
 * before it disappears.
 */
watch(waitingIds, (ids) => forget(ids), { immediate: true, flush: 'post' })

/**
 * What the card says it is doing, from the status rather than from the bar.
 *
 * `queued` and `processing` are genuinely different waits -- one is "no worker has taken this
 * yet", the other is "a model is running" -- and saying so is free.
 */
function waitLabel(status) {
  return status === 'processing' ? 'Transcribing…' : 'Waiting for a worker…'
}

/**
 * The reminder a card should badge itself with: the soonest one still owed.
 *
 * The API returns them soonest-first, so this is the first undelivered one rather than a
 * search for a minimum. Delivered ones are skipped because a bell on a memo whose alarm went
 * off yesterday says nothing about what is coming.
 */
function nextReminder(memo) {
  return memo.reminders?.find((reminder) => reminder.delivered_at === null) ?? null
}

/**
 * A reminder's time, in the reader's own zone and as short as it can be.
 *
 * Today's reminders show a time and nothing else, because the date would be noise on the one
 * that matters most. Anything further out shows a date, since "09:00" alone is worse than
 * useless for something three days away.
 */
function reminderLabel(reminder) {
  const at = new Date(reminder.remind_at)

  if (Number.isNaN(at.getTime())) {
    return reminder.remind_at
  }

  const now = new Date()
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate()

  return sameDay
    ? at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : at.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/**
 * The API sends RFC 3339 in UTC with a literal Z, so this is one of the few date strings
 * every browser parses identically, and it is shown in the reader's own zone.
 *
 * The fallback prints the string as it arrived, because "Invalid Date" tells whoever reads it
 * nothing about what came over the wire.
 */
function created(iso) {
  const at = new Date(iso)

  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString()
}

function shortCreated(iso) {
  const at = new Date(iso)

  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleDateString([], { month: 'short', day: 'numeric' })
}
</script>

<template>
  <p v-if="loading && memos.length === 0" class="notice">Loading…</p>

  <!--
    A <ul> inside the scroller rather than the scroller itself, so the scroll container is a
    plain element and the list keeps its semantics.

    tabindex="0" on the scroller is what makes it reachable by keyboard. A scroll container
    with focusable children can be scrolled by tabbing *through* them, but only as far as
    there are children to tab to -- and these cards are buttons, so that happens to work here.
    It is still wrong to rely on: a screen-reader user arrowing through the list is not
    tabbing, and without a focusable container there is no way to scroll it at all. The
    role/aria-label pair is what stops that focusable div being announced as an unlabelled
    group.
  -->
  <div
    v-else-if="memos.length > 0"
    class="strip"
    tabindex="0"
    role="region"
    aria-label="Scrollable memo list"
  >
    <ul class="strip__track">
      <li v-for="memo in memos" :key="memo.id" class="strip__item">
        <!--
          The whole card is one button, not a div with a click handler. It is an action -- it
          opens the detail card -- so it has to be focusable, activate on Enter and Space, and
          be announced as a control. A div with @click does none of those.
        -->
        <button type="button" class="memo-card" @click="emit('open', memo)">
          <span class="memo-card__top">
            <span class="badge" :class="`badge--${memo.status}`">{{ memo.status }}</span>

            <!--
              The bell is the whole point of carrying reminders on the list rows: without it,
              the only way to know a memo has an alarm set is to open it.
            -->
            <span v-if="nextReminder(memo)" class="memo-card__bell">
              <span aria-hidden="true">⏰</span>
              {{ reminderLabel(nextReminder(memo)) }}
            </span>
          </span>

          <!--
            The brief label. memoLabel picks the best short thing the memo has -- its enriched
            title once there is one, and something honest about the wait before that.
          -->
          <span class="memo-card__title">{{ memoLabel(memo) }}</span>

          <!--
            The transcript preview is clamped to a few lines in CSS rather than truncated
            here, so the card shows as much as fits at whatever width the screen gives it.
          -->
          <span v-if="memo.transcript" class="memo-card__preview">{{ memo.transcript }}</span>

          <!--
            A memo still being worked on gets the wait instead of "No transcript yet." — that
            sentence is a statement about a finished row, and under a running job it reads as a
            result rather than as a wait. A failed one with no transcript still gets it,
            because there it *is* the result.
          -->
          <span v-else-if="working(memo)" class="memo-card__preview memo-card__preview--empty">
            {{ waitLabel(memo.status) }}
          </span>

          <span v-else class="memo-card__preview memo-card__preview--empty">
            No transcript yet.
          </span>

          <time class="memo-card__when" :datetime="memo.created_at" :title="created(memo.created_at)">
            {{ shortCreated(memo.created_at) }}
          </time>
        </button>

        <!--
          Outside the button, not inside it, and that is an HTML rule rather than a layout
          choice: a <button>'s content model is phrasing content, and ProgressBar's root is a
          <div>. Browsers tolerate the nesting and then recover from it differently, which is a
          worse outcome than putting it here and positioning it over the card's lower edge --
          which is where it looked like it was anyway.

          The bar carries its own accessible name, so there is no caption beside it: the card
          already says "Transcribing…" in words above.
        -->
        <ProgressBar
          v-if="working(memo)"
          class="strip__progress"
          :label="waitLabel(memo.status)"
          :value="progressFor(memo.id)"
        />
      </li>
    </ul>
  </div>

  <!--
    Three different empty states, because answering all of them with "no memos" is a small lie
    that reads as a bug. A failed load makes no claim at all: the error banner above says what
    happened, and the rows that did load are still on screen.
  -->
  <p v-else-if="failed" class="notice">Nothing to show while the list cannot be loaded.</p>

  <p v-else-if="query !== null" class="notice">
    No memos match <strong>{{ query }}</strong
    ><template v-if="dateLabel"> in {{ dateLabel.toLowerCase() }}</template
    >. Try fewer words, or widen the dates.
  </p>

  <p v-else-if="dateLabel" class="notice">
    No memos from {{ dateLabel.toLowerCase() }}. Try a wider range.
  </p>

  <p v-else class="notice">{{ emptyHint }}</p>
</template>

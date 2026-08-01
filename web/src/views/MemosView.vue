<script setup>
import { onMounted, ref } from 'vue'
import CollectionDialog from '../components/CollectionDialog.vue'
import CollectionGrid from '../components/CollectionGrid.vue'
import ListFilters from '../components/ListFilters.vue'
import MemoComposer from '../components/MemoComposer.vue'
import MemoDialog from '../components/MemoDialog.vue'
import MemoRecorder from '../components/MemoRecorder.vue'
import MemoStrip from '../components/MemoStrip.vue'
import ReminderBanner from '../components/ReminderBanner.vue'
import { useCollections } from '../composables/useCollections'
import { useMemos } from '../composables/useMemos'
import { usePolling } from '../composables/usePolling'
import { startReminders } from '../composables/useReminders'

/*
 * The app: fast memos on top, collections underneath.
 *
 * This is what used to be App.vue, in the order the brief asks for. It is also the one file
 * that holds both singletons -- the memo strip and the collections grid -- which is why the
 * cross-cutting reloads live here: filing a memo changes the strip's membership *and* two
 * cards' counts, and deleting a collection hands its memos back to the strip. Neither
 * composable reaches into the other; this is the place that knows both exist.
 */

const {
  memos,
  pending,
  loading,
  busy,
  loadError,
  query,
  displayedFilter,
  dateRange,
  load,
  search,
  searchNow,
  clearSearch,
  applyDateRange,
} = useMemos()

const collections = useCollections()

/*
 * The one place the list and the timer are joined. `pending` is the stop condition and lives
 * with the statuses it reads; everything about when to fire lives in usePolling.
 *
 * This is the strip's timer. An opened collection runs its own, on the same schedule, from
 * inside CollectionDialog -- both hold memos a worker is still finishing, so both have to
 * watch for it.
 *
 * The collections *grid* is the one thing not polled, and that is a real difference rather
 * than an oversight: nothing but this app writes a collection, so there is no third party for
 * a timer to discover. It is reloaded when something is known to have changed.
 */
const { hinting } = usePolling(pending, () => load({ background: true }))

/** The memo whose card is open, or null. */
const openMemo = ref(null)

/** The collection whose contents are open, or null. */
const openCollection = ref(null)

/**
 * The open collection's dialog, so its memo list can be re-read after a memo moves.
 *
 * A template ref rather than another event, because this is the parent telling a child that
 * something happened elsewhere -- the opposite direction from the events above.
 */
const collectionDialog = ref(null)

/**
 * Open a memo's card.
 *
 * The object is held rather than the id, and it is the *same* object the list holds -- so when
 * a poll writes a new transcript into that row, the open card updates with it. Holding the id
 * and looking it up would work too; holding a copy would not, and is the mistake this note
 * exists to prevent.
 */
function showMemo(memo) {
  openMemo.value = memo
}

/**
 * Something about a memo changed: it moved, or its reminders did.
 *
 * The grid is reloaded because a move changes the counts and the recent labels on two cards
 * without touching either collection row -- so nothing else would notice. The strip reloads
 * itself inside moveMemo, since membership is its own business.
 */
function memoChanged() {
  collections.load()

  // And the open collection, if there is one. A move can take a memo *out of* the collection
  // being looked at, and membership is decided by the query rather than by a field, so the
  // memo's own object being up to date is not enough to drop it from that list.
  collectionDialog.value?.reload()
}

/**
 * Take the reader to the controls that add a fast memo, and put the cursor in the textarea.
 *
 * Focus is moved as well as scrolled, and the focus is the part that matters: scrolling alone
 * leaves a keyboard user exactly where they were, having pressed a button that appeared to do
 * nothing. Focusing the textarea also scrolls it into view on its own in every current
 * browser, so the explicit scroll is for the smooth behaviour rather than for the position.
 *
 * The textarea rather than the Record button, because typing is the one that needs a cursor
 * placed -- Record is a single keystroke away once focus is in that region, and putting focus
 * on it would mean a stray Space or Enter starts recording.
 *
 * Queried from the document rather than held as a template ref, because the element belongs to
 * MemoComposer. A ref would mean that component exposing its textarea purely so this button
 * could reach in, which is a worse coupling than one selector.
 */
function addFastMemo() {
  const composer = document.querySelector('.composer__text')

  composer?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  composer?.focus({ preventScroll: true })
}

/**
 * A collection was deleted: its memos are fast memos again.
 *
 * `ON DELETE SET NULL` does that inside Postgres, where no response can observe it, so the
 * strip has to be told to look again. Closing the dialog first, in case the collection being
 * deleted is the one open in it.
 */
function collectionsChanged() {
  openCollection.value = null

  load()
}

onMounted(() => {
  load()
  collections.load()

  // Idempotent, and deliberately never stopped -- see useReminders. Started here rather than
  // in App.vue because this is the screen that can set a reminder, and a loop running behind
  // the landing page before anyone has an account of what it is for would be odd.
  startReminders()
})
</script>

<template>
  <main class="app">
    <header class="app__header">
      <h1>Memos</h1>

      <!--
        Kept, now that the poll exists, because the poll deliberately stops: once every memo is
        `ready` or `failed` nothing on this screen is waiting for anything, and a timer left
        running against a finished list is a request every 5 seconds answering the same thing
        forever. A memo written from somewhere else -- a second tab, curl -- is then invisible
        until something asks, and this is what asks.
      -->
      <button type="button" :disabled="loading" @click="load()">
        {{ loading ? 'Refreshing…' : 'Refresh' }}
      </button>
    </header>

    <!--
      The recorder first, because it is what the app is for and because the bloom behind the
      page is centred on its button. Both write paths are here rather than behind the strip's
      "add" affordance: the brief asks for a button that adds a fast memo, and these two *are*
      that button -- a memo is created unfiled, so everything made here lands in the strip
      below.
    -->
    <MemoRecorder />

    <MemoComposer />

    <p v-if="hinting" class="notice" role="status">
      Still transcribing — a long recording can take a while.
    </p>

    <section class="section" aria-labelledby="fast-heading">
      <header class="section__head">
        <h2 id="fast-heading">Fast memos</h2>

        <!--
          The brief asks for an "add fast memo" button on this list, and the two controls that
          actually add one are above it rather than in here. That is deliberate -- the recorder
          has to stay at the top, because it is what the app is for and because the bloom is
          anchored to it -- so this is the affordance rather than a third way to write a memo.
          Every memo is created unfiled, so anything made up there lands in this strip; the
          button just takes you to the controls and puts the cursor in the box.

          A button and not a link: it moves focus within the page rather than navigating.
        -->
        <button type="button" class="ghost" @click="addFastMemo">+ Add fast memo</button>
      </header>

      <p class="section__hint">
        Everything not filed away yet. Open one to read it, set a reminder, or move it into a
        collection.
      </p>

      <ListFilters
        :query="query"
        :date-range="dateRange"
        placeholder="Search titles and transcripts…"
        label="Search fast memos"
        @search="search"
        @search-now="searchNow"
        @clear="clearSearch"
        @apply="applyDateRange"
      />

      <!--
        The list's own error, kept above the list rather than replacing it: a failed refresh
        leaves the rows that did load on screen, and blanking them would look like the memos
        were gone.
      -->
      <p v-if="loadError" class="notice notice--error" role="alert">{{ loadError }}</p>

      <!--
        `busy`, not `loading`. This component's job is to say why the list is empty, and a
        filter change that is still inside its debounce has not started a request yet -- so on
        `loading` it would answer that question from the previous filter's result.
      -->
      <MemoStrip
        :memos="memos"
        :loading="busy"
        :failed="Boolean(loadError)"
        :query="displayedFilter"
        :date-label="dateRange.isActive ? dateRange.label : null"
        empty-hint="No fast memos. Record one above, or they are all filed into collections."
        @open="showMemo"
      />
    </section>

    <CollectionGrid @open="openCollection = $event" @changed="collectionsChanged" />
  </main>

  <!--
    Both dialogs and the reminder banner sit outside <main>, which is deliberate for each. The
    dialogs render in the browser's top layer regardless of where they are declared, so putting
    them inside the landmark would only misdescribe the page; the banner is a notification
    surface rather than page content.
  -->
  <MemoDialog :memo="openMemo" @close="openMemo = null" @changed="memoChanged" />

  <CollectionDialog
    ref="collectionDialog"
    :collection="openCollection"
    @close="openCollection = null"
    @open-memo="showMemo"
  />

  <ReminderBanner />
</template>

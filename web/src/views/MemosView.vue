<script setup>
import { onMounted, onScopeDispose, ref } from 'vue'
import AskWidget from '../components/AskWidget.vue'
import CollectionDialog from '../components/CollectionDialog.vue'
import CollectionGrid from '../components/CollectionGrid.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ListFilters from '../components/ListFilters.vue'
import MemoComposer from '../components/MemoComposer.vue'
import MemoDialog from '../components/MemoDialog.vue'
import MemoRecorder from '../components/MemoRecorder.vue'
import MemoStrip from '../components/MemoStrip.vue'
import MemoToasts from '../components/MemoToasts.vue'
import OwnerPanel from '../components/OwnerPanel.vue'
import ReminderBanner from '../components/ReminderBanner.vue'
import { useCollections } from '../composables/useCollections'
import { onMemoRemoved } from '../composables/useMemoList'
import { useMemos } from '../composables/useMemos'
import { ensureOwner } from '../composables/useOwner'
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
 * Close the card if the memo it is showing stops existing.
 *
 * **Reachable, and it became much more so with the discard path.** Open a voice memo while it
 * is still transcribing, and if the worker finds nothing in it the row is deleted out from
 * under the dialog -- which goes on rendering it, with a Delete and a Rename that would both
 * 404. It was already reachable through a delete in a second tab; that was rare enough to have
 * gone unnoticed, and this is the same bug arriving on a path people will actually walk.
 *
 * Registered rather than watched, because "gone" is not something the list can express. A memo
 * leaves a list whenever a filter moves, and closing the card for that would be wrong -- the
 * memo is fine and the user is reading it. Only the code that removed it knows which happened,
 * which is the same argument useMemoToasts makes about `forgetMemo`.
 *
 * **Unsubscribed on unmount, and the first version was not.** It said this view lives for the
 * life of the page, which is wrong: router.js has two real routes, so every trip to the landing
 * page and back unmounts and remounts this component. Each visit would add another callback to
 * a module-scoped Set, each holding a `openMemo` ref belonging to a component that no longer
 * exists -- harmless to look at and unbounded in number.
 *
 * onScopeDispose rather than onUnmounted, because what this belongs to is the setup scope: the
 * registration happens here at setup time and should end when this scope does, whether that is
 * an unmount or a scope stopped some other way.
 */
onScopeDispose(
  onMemoRemoved((id) => {
    if (openMemo.value?.id === id) {
      openMemo.value = null
    }
  }),
)

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

onMounted(async () => {
  // **Awaited, and everything below depends on it having finished.** `GET /api/owner` is the
  // only safe read the API will create an identity for, so this is what guarantees the three
  // calls after it arrive with a cookie. Started in parallel instead, all four would arrive
  // without one: the API answers reads from an empty transient owner, so the screen would come
  // up blank, and the identity this call then mints is a different one from the one the next
  // reload would present. See composables/useOwner.js.
  //
  // It costs one round trip before the first list request on a cold load, and nothing on any
  // load after that -- the cookie is already there, so the API resolves it rather than
  // minting.
  await ensureOwner()

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
      Directly under the header and above everything that writes a memo, because what it has
      to say is a precondition for all of them: these memos belong to this browser. Collapsed
      by default -- it is reference rather than news -- but it is also where the notice after
      following a claim link appears, and that one has to be seen without being looked for.
    -->
    <OwnerPanel />

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

    <!--
      No "+ Add fast memo" button here any more.

      It was the brief's affordance for adding a memo to this list, and it never added one:
      the two controls that do are the Record button and the textarea, thirty pixels above,
      both permanently on screen. All it did was scroll to them and focus the box. On a screen
      where the thing it points at is already visible, a button that moves the cursor is a
      control the reader has to work out the purpose of, and the answer is "nothing you could
      not do by clicking the box".
    -->
    <section class="section" aria-labelledby="fast-heading">
      <header class="section__head">
        <h2 id="fast-heading">Fast memos</h2>
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
    Everything below sits outside <main>, which is deliberate for each of them. The three
    dialogs render in the browser's top layer regardless of where they are declared, so putting
    them inside the landmark would only misdescribe the page; the corner holds notification
    surfaces rather than page content.
  -->
  <MemoDialog :memo="openMemo" @close="openMemo = null" @changed="memoChanged" />

  <CollectionDialog
    ref="collectionDialog"
    :collection="openCollection"
    @close="openCollection = null"
    @open-memo="showMemo"
  />

  <!--
    One fixed corner, two groups inside it. The container lives here rather than in either
    component because there is one corner and two things that want it -- each owning its own
    `position: fixed` put them on top of each other, which is what this replaced.

    Memos above reminders, which is the order they are read in: a submission's card appears in
    response to something that happened a second ago and is looked at immediately, while a
    reminder can sit for a while. Below, it would be pushed around by every save.
  -->
  <!--
    One host for every "are you sure?" on this screen -- the memo delete and the collection
    delete both ask through useConfirm, and a dialog per caller would mean two <dialog>
    elements competing for the top layer. Last in the template so it opens above the sheets,
    which is also where the browser stacks it regardless: modal dialogs enter the top layer in
    the order showModal() is called.
  -->
  <ConfirmDialog />

  <!--
    Ask, in the bottom right. Outside <main> like the dialogs above it, and for the same
    reason: it floats over the page rather than sitting anywhere in it, so putting it inside
    the landmark would misdescribe the document.

    It used to be a section between the composer and the strip. What that cost was a band of
    the page permanently given to an empty box, a memo list that jumped down whenever an
    answer arrived, and a feature that could only be used from one scroll position -- see
    AskWidget for the longer version.

    `memos` is passed so a citation can open the card the strip is already holding rather than
    fetching a second copy of it -- see `showMemo` for why it has to be that object.
    Citations to memos the strip does *not* hold are fetched by id, which is what the widget
    needs `GET /api/memos/{id}` for.
  -->
  <AskWidget :memos="memos" @open-memo="showMemo" />

  <div class="toasts">
    <MemoToasts />

    <ReminderBanner />
  </div>
</template>

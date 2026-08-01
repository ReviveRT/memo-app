<script setup>
import { computed, onUnmounted, shallowRef, watch } from 'vue'
import ListFilters from './ListFilters.vue'
import MemoStrip from './MemoStrip.vue'
import { createMemoList } from '../composables/useMemoList'
import { usePolling } from '../composables/usePolling'

/*
 * An opened collection: everything filed in it, with the same search and date filter the fast
 * strip has.
 *
 * **This is the one place a second memo list exists, and the reason createMemoList is a
 * factory rather than a singleton.** Typing in here must not re-filter the strip behind it,
 * and the strip's date range is not this one's -- two lists, two sets of filter state, one
 * implementation.
 *
 * The list is built when a collection is opened and thrown away when another one is, rather
 * than one list being re-scoped. The scope is what identifies a list: re-pointing it would
 * leave the previous collection's memos on screen under the new collection's name for as long
 * as the fetch took, which is exactly the frame somebody screenshots.
 */

const props = defineProps({
  /** The collection to show, or null when the dialog is closed. */
  collection: { type: Object, default: null },
})

const emit = defineEmits(['close', 'open-memo'])

const dialogEl = shallowRef(null)

/**
 * The memo list for whichever collection is open.
 *
 * shallowRef rather than ref: this holds a bag of refs and functions, and making it deeply
 * reactive would have Vue walk and proxy every one of them on assignment for no benefit --
 * the inner refs are already reactive on their own.
 */
const list = shallowRef(null)

watch(
  () => props.collection,
  (collection) => {
    const el = dialogEl.value

    // Unregister the outgoing list before building its replacement. Without this every open
    // would leave a dead list in useMemoList's registry, still being written to by every
    // subsequent memo update, forever.
    list.value?.dispose()

    if (collection) {
      // A fresh list per collection. The old one is dropped whole, which also discards its
      // filters -- opening a collection should not inherit the search text from the last one.
      list.value = createMemoList({ collection: collection.id })
      list.value.load()

      // Guarded on `open`, because showModal() on an already-open dialog throws
      // InvalidStateError -- reachable by clicking a second collection while one is open.
      if (el && !el.open) {
        el.showModal()
      }
    } else {
      list.value = null

      if (el?.open) {
        el.close()
      }
    }
  },
  { flush: 'post' },
)

/*
 * This list is polled too, on the same timer the fast strip uses.
 *
 * It was not, and the omission was an inconsistency rather than a decision: a memo filed into a
 * collection while it was still being transcribed would sit in this dialog saying
 * "Transcribing…" forever, while the identical memo in the strip behind it filled itself in.
 * One list quietly behaving differently from the other is exactly the kind of thing nobody
 * reports as a bug and everybody notices.
 *
 * `pending` is false while no collection is open -- there is no list at all then -- so the
 * timer stops on its own when the dialog closes, without this component having to say so.
 */
const pending = computed(() => list.value?.pending.value ?? false)

usePolling(pending, () => list.value?.load({ background: true }))

/**
 * Re-read this collection's memos.
 *
 * Exposed so MemosView can call it after a memo is filed or unfiled from the detail card. That
 * write can move a memo *out of the collection currently open here*, and nothing else would
 * notice: the memo's own object is brought up to date by the write, but membership of this
 * list is decided by the query rather than by any field, so only a reload can drop it.
 *
 * A parent calling a child through a ref rather than the child watching a counter prop,
 * because the parent is the only one that knows a write happened and there is nothing to
 * derive it from.
 */
function reload() {
  list.value?.load()
}

// The component itself is never unmounted in practice -- it sits in MemosView's template --
// but a list left registered after teardown is the kind of leak that only shows up once
// something else changes, so it is released explicitly.
onUnmounted(() => list.value?.dispose())

defineExpose({ reload })
</script>

<template>
  <dialog
    ref="dialogEl"
    class="sheet sheet--wide"
    @close="emit('close')"
    @click="$event.target === dialogEl && emit('close')"
  >
    <article v-if="collection && list" class="sheet__body">
      <header class="sheet__head">
        <div>
          <h2 class="sheet__title">{{ collection.name }}</h2>

          <p class="sheet__meta">
            {{ collection.memo_count === 1 ? '1 memo' : `${collection.memo_count} memos` }}
          </p>
        </div>

        <button type="button" class="sheet__close" aria-label="Close" @click="emit('close')">
          ×
        </button>
      </header>

      <ListFilters
        :query="list.query.value"
        :date-range="list.dateRange"
        placeholder="Search in this collection…"
        :label="`Search memos in ${collection.name}`"
        @search="list.search"
        @search-now="list.searchNow"
        @clear="list.clearSearch"
        @apply="list.applyDateRange"
      />

      <p v-if="list.loadError.value" class="notice notice--error" role="alert">
        {{ list.loadError.value }}
      </p>

      <!--
        The same strip component the fast memos use. A collection's contents are memos in a
        row that scrolls, exactly as the unfiled ones are, and giving them a different
        arrangement would suggest they were a different kind of thing.
      -->
      <MemoStrip
        :memos="list.memos.value"
        :loading="list.busy.value"
        :failed="Boolean(list.loadError.value)"
        :query="list.displayedFilter.value"
        :date-label="list.dateRange.isActive ? list.dateRange.label : null"
        empty-hint="Nothing filed here yet. Open a fast memo and choose this collection."
        @open="emit('open-memo', $event)"
      />
    </article>
  </dialog>
</template>

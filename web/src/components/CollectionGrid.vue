<script setup>
import { ref } from 'vue'
import CollectionCard from './CollectionCard.vue'
import ListFilters from './ListFilters.vue'
import { useCollections } from '../composables/useCollections'

/*
 * The grid of collections: a search box, a create field, and the cards.
 *
 * **The layout the brief asks for is "columns from the screen width, at most three rows, then
 * scroll down for the rest", and it is expressed entirely in CSS** -- `repeat(auto-fill,
 * minmax(...))` for the columns, and a max-height of three card heights plus two gaps with
 * `overflow-y: auto` for the cap. Nothing here counts cards or measures anything, which is
 * what keeps it correct while the window is being resized: the browser reflows the columns and
 * the same three-row height keeps meaning three rows. See `.grid` in styles.css, where the
 * card height is the shared custom property that makes the arithmetic possible.
 */

const emit = defineEmits(['open', 'changed'])

const {
  collections,
  busy,
  saving,
  loadError,
  saveError,
  query,
  displayedFilter,
  dateRange,
  search,
  searchNow,
  clearSearch,
  applyDateRange,
  create,
  rename,
  remove,
} = useCollections()

const newName = ref('')

async function submitNew() {
  const name = newName.value.trim()

  // Guarded here as well as by the disabled attribute, because Enter reaches this without
  // going through the button.
  if (name === '' || saving.value) {
    return
  }

  if (await create(name)) {
    newName.value = ''
  }

  // Not cleared on failure: a name refused as a duplicate is still the name the user typed,
  // and blanking it would make them retype it to find out what else is wrong with it.
}

async function onRename(id, name) {
  await rename(id, name)
}

async function onDelete(id) {
  if (await remove(id)) {
    // The memos this was holding have just become fast memos, and the strip is the parent's.
    emit('changed')
  }
}
</script>

<template>
  <section class="section" aria-labelledby="collections-heading">
    <header class="section__head">
      <h2 id="collections-heading">Collections</h2>
    </header>

    <p class="section__hint">
      Group related memos — “Memos for Work”, “Groceries”. Open any memo to file it into one.
    </p>

    <!--
      The same filter component the memo lists use, wired to this grid's own state. Searching
      here reaches further than the name: the API also matches the memos filed inside each
      collection, so "dentist" finds the collection holding the dentist memo. The placeholder
      says so, because a search box that quietly does more than it claims is a feature nobody
      finds.
    -->
    <ListFilters
      :query="query"
      :date-range="dateRange"
      placeholder="Search collections and what is in them…"
      label="Search collections"
      @search="search"
      @search-now="searchNow"
      @clear="clearSearch"
      @apply="applyDateRange"
    />

    <form class="creator" @submit.prevent="submitNew">
      <label class="creator__field">
        <span class="sr-only">New collection name</span>
        <input
          v-model="newName"
          type="text"
          maxlength="120"
          placeholder="New collection name…"
          :disabled="saving"
        />
      </label>

      <button type="submit" :disabled="saving || newName.trim() === ''">
        {{ saving ? 'Saving…' : 'Create collection' }}
      </button>
    </form>

    <!--
      Kept above the grid rather than replacing it, for the reason the memo list's error is: a
      failed refresh leaves the cards that did load on screen, and blanking them would look
      like the collections were gone.
    -->
    <p v-if="loadError" class="notice notice--error" role="alert">{{ loadError }}</p>
    <p v-if="saveError" class="notice notice--error" role="alert">{{ saveError }}</p>

    <p v-if="busy && collections.length === 0" class="notice">Loading…</p>

    <!--
      role="list" restored explicitly, because `list-style: none` makes Safari drop the list
      semantics from a <ul> -- a documented VoiceOver behaviour, and this list is styled that
      way. Without it the cards are announced as loose content with no count.
    -->
    <ul v-else-if="collections.length > 0" class="grid" role="list">
      <li v-for="collection in collections" :key="collection.id">
        <CollectionCard
          :collection="collection"
          :saving="saving"
          @open="emit('open', $event)"
          @rename="onRename"
          @delete="onDelete"
        />
      </li>
    </ul>

    <p v-else-if="displayedFilter !== null" class="notice">
      No collections match <strong>{{ displayedFilter }}</strong
      >.
    </p>

    <p v-else-if="dateRange.isActive" class="notice">
      No collections made {{ (dateRange.label ?? '').toLowerCase() }}.
    </p>

    <p v-else class="notice">No collections yet. Make one above to start grouping memos.</p>
  </section>
</template>

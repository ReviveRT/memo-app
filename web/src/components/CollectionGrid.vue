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
const nameEl = ref(null)

/**
 * Why the last press of Create did nothing, or null.
 *
 * **This exists because the button used to be disabled on an empty name, and that was
 * reported as the button being broken.** It is worth spelling out, because the fix is not
 * the obvious one. `button:disabled` was `opacity: 0.5` and nothing else, and on the dark
 * scheme a half-opacity accent fill is still a solid blue button -- indistinguishable from
 * a live one at a glance. So the control looked pressable, was pressed, and did nothing,
 * with no disabled cursor to contradict it and no message to explain it. Worse, the field
 * it wants filled sits directly under a search box of the same width and shape, so the
 * commonest way to arrive at that state is to type the name into the wrong one of two
 * identical inputs and never learn that you did.
 *
 * Two changes rather than one, because either alone leaves half of it standing. Disabled
 * buttons now look disabled (see styles.css), *and* this button is no longer disabled for
 * an empty name -- it is always pressable, and pressing it says what it wants. A control
 * that answers is better than one that cannot be pressed, and it is the only one of the two
 * that helps somebody who has typed their collection name into the search box.
 *
 * @type {import('vue').Ref<?string>}
 */
const nameProblem = ref(null)

async function submitNew() {
  const name = newName.value.trim()

  // `saving` still guards, because that one is a genuine "not yet" rather than a "you have
  // not said what to call it": the request in flight is about to reload the grid underneath.
  if (saving.value) {
    return
  }

  if (name === '') {
    // Focus as well as the message. Somebody who typed the name into the search box above is
    // looking at their own words on the screen while being told the field is empty, and the
    // cursor landing in the right box is the part of that which explains it.
    nameProblem.value = 'Type a name for the collection first.'
    nameEl.value?.focus()

    return
  }

  nameProblem.value = null

  if (await create(name)) {
    newName.value = ''
  }

  // Not cleared on failure: a name refused as a duplicate is still the name the user typed,
  // and blanking it would make them retype it to find out what else is wrong with it.
}

/** Typing is the answer to the hint, so it clears it rather than waiting for another press. */
function onNameInput() {
  if (nameProblem.value !== null && newName.value.trim() !== '') {
    nameProblem.value = null
  }
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

    <!--
      A visible legend, where there used to be an sr-only label and a placeholder.
      <fieldset> rather than a heading, because that is the element for "these controls
      belong together" and its <legend> is announced with each of them -- so the field is
      named even once the placeholder has been typed over, which is exactly when somebody
      would otherwise lose track of which of the two boxes on this screen they are in.

      The pair above it is a search box of the same width and the same shape. That is the
      whole reason this is labelled out loud now: two identical inputs, one under the other,
      with only their placeholder text to tell them apart, and the placeholder disappears the
      moment either is used.
    -->
    <form class="creator" @submit.prevent="submitNew">
      <fieldset class="creator__group">
        <legend class="creator__legend">New collection</legend>

        <label class="creator__field">
          <span class="sr-only">Collection name</span>
          <input
            ref="nameEl"
            v-model="newName"
            type="text"
            maxlength="120"
            placeholder="Groceries, Work, Ideas…"
            :disabled="saving"
            :aria-describedby="nameProblem ? 'creator-problem' : undefined"
            @input="onNameInput"
          />
        </label>

        <!--
          Disabled only while a write is in flight. Not on an empty name: see nameProblem for
          why an inert button was the reported bug rather than the guard against it.
        -->
        <button type="submit" :disabled="saving">
          {{ saving ? 'Saving…' : 'Create collection' }}
        </button>
      </fieldset>

      <!--
        role="alert" rather than a plain paragraph, because it appears in response to a press
        and the press is the only thing that would have moved a screen reader's attention
        anywhere near it.
      -->
      <p v-if="nameProblem" id="creator-problem" class="notice notice--error" role="alert">
        {{ nameProblem }}
      </p>
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

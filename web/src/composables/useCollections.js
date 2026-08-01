import { computed, ref } from 'vue'
import {
  createCollection,
  deleteCollection,
  listCollections,
  renameCollection,
} from '../api/collections'
import { useDateRange } from './useDateRange'

/*
 * The collections grid: the cards, their search box, and the four writes.
 *
 * A singleton, unlike createMemoList, and the difference is not inconsistency. There can be
 * two memo lists on screen -- the strip and an opened collection -- so that one had to
 * become a factory. There is exactly one grid of collections and there is nowhere for a
 * second one to go, so module scope is the honest shape: it also means the memo detail
 * card's "move to" menu reads the same collections the grid is showing, without either of
 * them being handed the other.
 *
 * No poll. Nothing changes a collection except this app -- there is no worker enriching them
 * and no second writer -- so the grid is reloaded when something is known to have changed
 * rather than on a timer. The one case that needs care is a memo being filed or unfiled,
 * which changes a card's count and labels without touching the collection row at all;
 * MemosView reloads this after every move for exactly that reason.
 */

/** @type {import('vue').Ref<Array<object>>} */
const collections = ref([])

const loading = ref(false)
const saving = ref(false)
const loadError = ref(null)

/**
 * Where a failed create, rename or delete is reported.
 *
 * Separate from `loadError` for the reason useMemos keeps its errors apart: a grid that
 * could not be refreshed and a name that is already taken are different problems with
 * different remedies, and one ref would blank whichever came first.
 *
 * The message the user sees for a duplicate name is the API's -- "You already have a
 * collection called ..." -- because CollectionController words it for a reader rather than
 * for a log, and request() renders `message` verbatim.
 */
const saveError = ref(null)

/** What is in the grid's search box. */
const query = ref('')

/** The grid's own date filter, independent of the memo lists' -- see useDateRange. */
const dateRange = useDateRange()

/** The filter the cards on screen came back for, as the API reported it. */
const appliedQuery = ref(null)

/**
 * The filter the grid should be described as being under.
 *
 * The same split useMemoList uses and for the same reason: the box knows whether a filter is
 * in effect because it is ahead of the network, and the API's echo knows what to call the
 * cards on screen because they are what it answered.
 */
const displayedFilter = computed(() => (query.value.trim() === '' ? null : appliedQuery.value))

/**
 * Matches the memo list's debounce, deliberately. Two search boxes on one screen that
 * settled at different speeds would read as one of them being slower rather than as a
 * choice.
 */
const DEBOUNCE_MS = 250

const debouncing = ref(false)

/** True while what is on screen is not the answer to the current filter. */
const busy = computed(() => loading.value || debouncing.value)

let debounce = null
let inFlight = false
let reloadWanted = false

function activeQuery() {
  const trimmed = query.value.trim()

  return trimmed === '' ? null : trimmed
}

/**
 * GET the grid.
 *
 * Replaced wholesale rather than merged by id, which is the opposite of what the memo list
 * does -- and the reason the memo list merges does not apply here. That merge exists because
 * the list is polled every couple of seconds and a wholesale replace would re-render fifty
 * rows to conclude nothing had changed. This is not polled: it loads when something has
 * actually changed, so every load is a load that has news.
 */
async function load() {
  if (inFlight) {
    reloadWanted = true

    return
  }

  inFlight = true
  loading.value = true

  try {
    const page = await listCollections({
      query: activeQuery(),
      from: dateRange.from,
      to: dateRange.to,
    })

    collections.value = page.collections
    appliedQuery.value = page.query
    loadError.value = null
  } catch (error) {
    loadError.value = `Could not load collections — ${error.message}`
  } finally {
    inFlight = false
    loading.value = false

    if (reloadWanted) {
      reloadWanted = false

      // Not awaited, for the reason useMemoList gives about its own deferred reload: this
      // runs inside the finally of the load that just finished, so awaiting would keep that
      // call on the stack for the length of the chain.
      load()
    }
  }
}

/** Type into the grid's search box. Debounced. */
function search(next) {
  query.value = next

  clearTimeout(debounce)
  debouncing.value = true

  debounce = setTimeout(() => {
    debounce = null
    debouncing.value = false
    load()
  }, DEBOUNCE_MS)
}

/** Search now, without waiting out the debounce. */
function searchNow() {
  clearTimeout(debounce)
  debounce = null
  debouncing.value = false

  load()
}

function clearSearch() {
  query.value = ''

  searchNow()
}

/** Change the date filter and reload. Not debounced -- a preset is one click. */
function applyDateRange(...args) {
  dateRange.set(...args)

  searchNow()
}

/**
 * One collection write: run it, report it, reload the grid.
 *
 * Reloaded rather than patched in place, because every one of these changes what the grid
 * shows in a way the response alone cannot express -- a create changes the order, a delete
 * changes membership, and a rename can move a card out of an active filter. A create could
 * be prepended, and deliberately is not: it would be right only while the grid is
 * unfiltered, and wrong in a way that looks like the new collection failing to save.
 *
 * @returns {Promise<boolean>} Whether it succeeded, so a form can clear itself only then.
 */
async function write(action, failure) {
  if (saving.value) {
    return false
  }

  saving.value = true

  try {
    await action()

    saveError.value = null

    await load()

    return true
  } catch (error) {
    saveError.value = `${failure} — ${error.message}`

    return false
  } finally {
    saving.value = false
  }
}

/**
 * Create a collection.
 *
 * Trimmed here as well as by the API, which is agreement rather than reliance: the API's
 * uniqueness index folds case and padding, so " Work " and "work" are the same collection,
 * and trimming on the way out means the name that is *stored* is the one the card will show.
 *
 * @param {string} name
 */
function create(name) {
  return write(() => createCollection(name.trim()), 'Could not create the collection')
}

/**
 * Rename one.
 *
 * Submitting the unchanged name is a successful no-op rather than a duplicate -- the unique
 * index compares the row against itself -- which matters because the rename field starts out
 * holding the current name.
 */
function rename(id, name) {
  return write(() => renameCollection(id, name.trim()), 'Could not rename the collection')
}

/**
 * Delete one. Its memos survive and return to the fast strip.
 *
 * The caller has to reload the memo list afterwards, and MemosView does: the strip has just
 * grown by however many memos this was holding, and nothing in the 204 says how many. That
 * is `ON DELETE SET NULL` on `memos.collection_id` doing the work inside Postgres, where no
 * response can observe it.
 */
function remove(id) {
  return write(() => deleteCollection(id), 'Could not delete the collection')
}

export function useCollections() {
  return {
    collections,
    loading,
    busy,
    saving,
    loadError,
    saveError,
    query,
    displayedFilter,
    dateRange,

    load,
    search,
    searchNow,
    clearSearch,
    applyDateRange,
    create,
    rename,
    remove,
  }
}

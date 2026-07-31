import { computed, ref } from 'vue'
import { createMemo, listMemos } from '../api/memos'

/*
 * The memo list, and the two things that change it.
 *
 * The state is declared at module scope rather than inside useMemos(), which is what
 * makes this one shared array instead of a fresh one per calling component: the
 * composer needs to prepend to the same list MemoList renders, and it should not have
 * to reach it through props and events routed via App.vue.
 *
 * No Pinia. A store would buy devtools time-travel and module namespacing for an app
 * whose entire state is the seven refs below, and it fails the same over-engineering
 * test this project applies to every other dependency. The shape a store would give
 * is already here -- state outside the component tree, mutated only by the functions
 * in this file -- so swapping it in later is a mechanical change if there is ever a
 * reason to.
 */

/**
 * Newest first, which is the order the API returns and the order the composer
 * maintains when it prepends.
 *
 * @type {import('vue').Ref<Array<object>>}
 */
const memos = ref([])

const loading = ref(false)
const saving = ref(false)

/**
 * Two error refs, not one. A failed refresh must not blank the message explaining why
 * the memo the user just typed was rejected, and a rejected memo must not masquerade as
 * a broken list.
 *
 * Both are prefixed with the operation that failed, which is not decoration: a stopped
 * api container fails the GET and the POST with the same sentence, and the two messages
 * then render one under the other with nothing to tell them apart.
 */
const loadError = ref(null)
const saveError = ref(null)

/**
 * What is in the search box. Bound by MemoSearch, and the only thing that decides
 * whether the next GET is filtered.
 *
 * @type {import('vue').Ref<string>}
 */
const query = ref('')

/**
 * The filter the rows currently on screen came back for, as the API reported it -- not
 * what is in the box.
 *
 * The two differ for as long as a keystroke is debounced or a request is in flight, and
 * the distinction is what keeps the empty state honest: "No memos match X" has to name
 * the query that produced the empty list, not the one the user has since typed half of.
 *
 * @type {import('vue').Ref<?string>}
 */
const appliedQuery = ref(null)

/**
 * The filter the list should be *described* as being under, or null for "not filtered".
 *
 * Not exported as two separate facts, because both places that describe a filtered list
 * -- the status line in MemoSearch and the empty state in MemoList -- have to agree about
 * when one is in effect, and they were not. The status line was corrected to hide the
 * moment the box empties; the empty state was left reading `appliedQuery` alone and went
 * on saying "No memos match xylophone" under an empty box for as long as the request to
 * unfilter took. One derived value, read by both, is what stops that drifting again.
 *
 * The box decides *whether* a filter is in effect and the API's echo decides *what to call
 * it*. Each is authoritative for its half: the box is ahead of the network, so it is the
 * only thing that knows the filter has been dropped, while the rows on screen belong to
 * whatever the API last answered -- naming them after the box would caption them with a
 * query they were not the answer to.
 *
 * @type {import('vue').ComputedRef<?string>}
 */
const displayedFilter = computed(() => (query.value.trim() === '' ? null : appliedQuery.value))

/**
 * Long enough that ordinary typing produces one request instead of one per character,
 * short enough that the list feels attached to the box. The API is same-origin through
 * the dev proxy, so there is no round trip worth hiding behind a longer wait.
 */
const DEBOUNCE_MS = 250

/**
 * Whether a filter change has been typed but its request has not started yet.
 *
 * This exists because `loading` alone leaves a hole exactly the width of the debounce, and
 * MemoList decides what to say about an empty list by asking whether one is in flight. In
 * that window nothing is in flight and the list is already out of date, so an empty list
 * gets described with certainty from a request that has not happened: backspace the last
 * character of a filter that matched nothing and the page states, for a quarter of a
 * second, that there are no memos at all -- the one claim that component's own comment
 * says it must never make falsely.
 *
 * @type {import('vue').Ref<boolean>}
 */
const pending = ref(false)

/**
 * "The list on screen is not the answer to the current filter, and something is on its way."
 * True across both halves of that: the debounce and the request.
 *
 * Kept separate from `loading` rather than folded into it, because the two answer different
 * questions. The Refresh button reflects a request actually running, so it stays on
 * `loading` -- disabling it during a debounce would be a button greyed out by somebody
 * typing in a different field.
 *
 * @type {import('vue').ComputedRef<boolean>}
 */
const busy = computed(() => loading.value || pending.value)

let debounce = null

/** Set when a load is asked for while one is running. See load(). */
let reloadWanted = false

/**
 * The box's contents as the API wants them: trimmed, and null rather than empty.
 *
 * Trimmed here and again by ListMemosRequest, which is agreement rather than reliance --
 * without the client-side half, a trailing space from a paste would be sent as part of
 * the filter and the ILIKE pattern would be `%dentist %`.
 */
function activeQuery() {
  const trimmed = query.value.trim()

  return trimmed === '' ? null : trimmed
}

/**
 * GET the list and replace it wholesale.
 *
 * The in-flight guard is not about load: it is about two responses. Overlapping GETs
 * resolve in whatever order the network decides, so the last one to arrive wins --
 * which can be the older answer. That was reachable by double-clicking Refresh, and
 * searching makes it routine rather than exotic: every debounced keystroke wants a
 * request, and the one that returns last decides what is on screen.
 *
 * So the guard now defers rather than discards. Returning early was fine while the only
 * caller was a button -- dropping a duplicate refresh costs nothing -- but a dropped
 * search is a list stuck showing the results for a query the user has already changed.
 * `reloadWanted` makes the last request always happen: at most one GET is in flight, and
 * whatever was asked for meanwhile runs the moment it lands. One request at a time is
 * also what removes the need to version responses, because two answers can never be in
 * the air to arrive out of order.
 */
async function load() {
  if (loading.value) {
    reloadWanted = true

    return
  }

  loading.value = true

  try {
    const page = await listMemos(activeQuery())

    memos.value = page.memos
    appliedQuery.value = page.query
    loadError.value = null
  } catch (error) {
    loadError.value = `Could not load memos — ${error.message}`
  } finally {
    loading.value = false

    if (reloadWanted) {
      reloadWanted = false

      // Not awaited, and it must not be: this is inside the finally of the load that
      // just finished, so awaiting here would keep that call on the stack for as long
      // as the chain of follow-ups lasts.
      load()
    }
  }
}

/**
 * Type into the filter. Debounced, so holding a key down is one request at the end
 * rather than one per repeat.
 *
 * @param {string} next
 */
function search(next) {
  query.value = next

  clearTimeout(debounce)

  // Set before the timer, not inside it: the whole point is to cover the wait.
  pending.value = true

  debounce = setTimeout(() => {
    debounce = null
    pending.value = false
    load()
  }, DEBOUNCE_MS)
}

/**
 * Filter now, without waiting out the debounce -- what Enter and the clear button do.
 *
 * The pending timer is cancelled rather than left to fire, so pressing Enter mid-debounce
 * results in one request instead of two identical ones a quarter of a second apart.
 */
function searchNow() {
  clearTimeout(debounce)
  debounce = null

  // The wait is over rather than cancelled -- load() takes over from here, and leaving this
  // set would strand `busy` true for good if that load returned before Vue next rendered.
  pending.value = false

  load()
}

/** Empty the box and show everything again. */
function clearSearch() {
  query.value = ''

  searchNow()
}

/**
 * POST one text memo and put it at the top of the list.
 *
 * Prepending the returned row rather than re-running load(): the API answers 201 with
 * the stored memo precisely so the client does not need a second round trip, and the
 * row it returns is the same shape the list carries (api/app/Services/Memos/Memo.php
 * exists to guarantee that). It is also not optimistic -- nothing appears until the
 * database has the row -- so there is no rollback path to get wrong.
 *
 * The one thing this does not survive is a GET that was already in flight when the POST
 * landed: that response replaces the array wholesale and predates the new row, so the
 * memo disappears from the screen until the next load, having been stored the whole
 * time. Reachable by hitting Refresh and submitting in the same breath. Left alone
 * rather than papered over with a generation counter -- MEMO-18 replaces the page by id
 * instead of wholesale, which closes it properly and is the task that owns the polling
 * this would matter for.
 *
 * Prepending is also what keeps the new memo on screen while a filter is active, and it
 * agrees with what the API would have answered rather than working around it: a memo
 * that has not been enriched yet is pinned into every filtered page regardless of match
 * (MemoRepository::search), so the row this puts at the top is the row the next GET
 * brings back. Once the worker finishes with it, a memo that does not match the filter
 * drops out -- which is the filter working, not the memo being lost.
 *
 * @param {string} text
 * @returns {Promise<boolean>} Whether the memo was stored. The composer clears the
 *   textarea only on true, so a rejected memo is still there to fix and resubmit.
 */
async function submit(text) {
  if (saving.value) {
    return false
  }

  saving.value = true

  try {
    // Trimmed before it goes out, so the string the composer judged against the
    // length cap is the string the API is asked to store. StoreMemoRequest trims
    // again -- that is agreement, not reliance -- and the composer's own guard is
    // what refuses a whitespace-only memo before it ever reaches here.
    memos.value = [await createMemo(text.trim()), ...memos.value]
    saveError.value = null

    return true
  } catch (error) {
    saveError.value = `Could not save the memo — ${error.message}`

    return false
  } finally {
    saving.value = false
  }
}

export function useMemos() {
  return {
    memos,
    loading,
    busy,
    saving,
    loadError,
    saveError,
    query,

    // `appliedQuery` itself is deliberately not exported. It is the raw echo, and a
    // component reading it directly is the bug displayedFilter exists to prevent.
    displayedFilter,

    load,
    search,
    searchNow,
    clearSearch,
    submit,
  }
}

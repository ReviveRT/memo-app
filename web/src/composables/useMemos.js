import { computed, ref } from 'vue'
import { createMemo, listMemos } from '../api/memos'

/*
 * The memo list, and the four things that change it: the first load, the Refresh button,
 * the poll (MEMO-18) and the filter (MEMO-19).
 *
 * The state is declared at module scope rather than inside useMemos(), which is what
 * makes this one shared array instead of a fresh one per calling component: the
 * composer needs to prepend to the same list MemoList renders, and it should not have
 * to reach it through props and events routed via App.vue.
 *
 * No Pinia. A store would buy devtools time-travel and module namespacing for an app
 * whose entire state is the nine refs below, and it fails the same over-engineering
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

/**
 * The two statuses nothing further happens to. 001_init.sql allows four; the other two
 * are `queued` and `processing`.
 *
 * Stated as the terminal set and negated, rather than as the non-terminal set and
 * tested directly, and the direction is the whole point of MEMO-18. The design this
 * task replaced polled "while any memo is queued" -- a positive list, one value short.
 * The worker's first act is queued -> processing, so that poll stopped within a tick
 * and the browser never heard about the transcript. Any positive list has the same
 * failure available to it the moment a status is added: MEMO-16 introduces a retry
 * path, and a status this file has never heard of must keep the page live rather than
 * freeze it. Unknown means not finished.
 */
const TERMINAL_STATUSES = new Set(['ready', 'failed'])

/**
 * Whether any memo on screen is still going to change. This is the poll's entire stop
 * condition -- see App.vue, which hands it to usePolling() -- and MemoSearch also reads
 * it, to say out loud that a row not containing the filter text is there on purpose.
 *
 * "On screen" is meant literally: it reads the page the API returned, so a memo past the
 * limit is not something this waits for.
 *
 * This used to say "or filtered out by MEMO-19's search" alongside that, and MEMO-19
 * landed without the case ever existing: a non-terminal memo cannot be filtered out,
 * because the search pins every one of them into the page regardless of match
 * (MemoRepository::search). So a filter can no longer strand the poll -- start a memo
 * transcribing, type a filter it does not match, and the row stays visible and the timer
 * stays running until it is done. That is the pin and the poll agreeing rather than a
 * coincidence: both are answering "is anything still owed work?", from the same page.
 */
const pending = computed(() => memos.value.some((memo) => !TERMINAL_STATUSES.has(memo.status)))

/**
 * Only true for a load somebody asked for -- the first one and the Refresh button.
 *
 * A poll tick deliberately leaves it alone. It is read by the button's label and by
 * MemoList's "Loading…" placeholder, and both are answers to "you asked for this, it
 * is happening"; a timer firing every 2 seconds is not a question anybody asked. Wired
 * to the tick it would flicker the button through "Refreshing…" and grey it out on
 * every interval, for the few milliseconds a GET takes against localhost.
 */
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
 *
 * A failed poll tick sets loadError like any other load, and the next successful tick
 * clears it. That is a banner that can appear and vanish on its own while nobody is
 * touching anything, which is correct -- an api container that went away should not be
 * hidden because the request that found out was on a timer -- but it is also the reason
 * a single blip now flashes red for one interval. Smoothing that over consecutive
 * failures belongs with MEMO-17, which owns failure UX and has the retry affordance to
 * put next to it.
 */
const loadError = ref(null)
const saveError = ref(null)

/**
 * What is in the search box. Bound by MemoSearch, and the only thing that decides whether
 * the next GET is filtered -- including a poll tick's, which is what keeps the timer on
 * the page the user is actually looking at.
 *
 * @type {import('vue').Ref<string>}
 */
const query = ref('')

/**
 * The filter the rows currently on screen came back for, as the API reported it -- not
 * what is in the box.
 *
 * The two differ for as long as a keystroke is debounced or a request is in flight, and
 * the distinction is what keeps the empty state honest: "No memos match X" has to name the
 * query that produced the empty list, not the one the user has since typed half of.
 *
 * @type {import('vue').Ref<?string>}
 */
const appliedQuery = ref(null)

/**
 * The filter the list should be *described* as being under, or null for "not filtered".
 *
 * Not exported as two separate facts, because both places that describe a filtered list --
 * the status line in MemoSearch and the empty state in MemoList -- have to agree about when
 * one is in effect, and while this task was being built they did not. The status line was
 * corrected to hide the moment the box empties; the empty state was left reading
 * `appliedQuery` alone and went on naming a filter under an emptied box. One derived value,
 * read by both, is what stops that drifting again.
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
 * short enough that the list feels attached to the box. The API is same-origin through the
 * dev proxy, so there is no round trip worth hiding behind a longer wait.
 */
const DEBOUNCE_MS = 250

/**
 * Whether a filter change has been typed but its request has not started yet.
 *
 * Named for the wait rather than for the work, because `pending` above is taken and means
 * something else entirely -- that the *server* still owes a memo something. This is the
 * client waiting on its own timer.
 *
 * It exists because `loading` leaves a hole exactly the width of the debounce, and
 * MemoList decides what to say about an empty list by asking whether a fresher one is
 * coming. In that window nothing is in flight and the list is already out of date, so an
 * empty list gets described with certainty from a request that has not happened: backspace
 * the last character of a filter that matched nothing and the page states, for a quarter of
 * a second, that there are no memos at all -- the one claim MemoList's own comment says it
 * must never make falsely. Measured before the fix, on a database holding five.
 *
 * @type {import('vue').Ref<boolean>}
 */
const debouncing = ref(false)

/**
 * "The list on screen is not the answer to the current filter, and something is on its
 * way." True across both halves of that: the debounce and the request.
 *
 * Kept separate from `loading` rather than folded into it, because the two answer different
 * questions -- and `loading` has already been narrowed once, by MEMO-18, so that a poll
 * tick does not flicker the Refresh button. Widening it here would undo that: the button
 * would grey out on every keystroke in a different field. So `loading` stays "a load
 * somebody asked for is running" and this is "what is on screen is not the answer yet".
 *
 * @type {import('vue').ComputedRef<boolean>}
 */
const busy = computed(() => loading.value || debouncing.value)

/** The pending debounce timer, or null when nothing is scheduled. */
let debounce = null

/**
 * Set when an interactive load is dropped by the in-flight guard. See load().
 */
let reloadWanted = false

/**
 * Guards the GET against overlapping with itself. Not a ref, because nothing renders
 * it: `loading` used to serve both purposes and can no longer, now that a background
 * tick has to take the guard without touching the label.
 */
let inFlight = false

/**
 * Counts writes this client has already applied to `memos`.
 *
 * The problem it solves is ordering, not identity, and it is the half that replacing
 * the page by id does not cover. A GET issued before a POST lands describes a list the
 * new memo was not in yet; merging that response by id still drops the row, because
 * membership comes from the response and the response is simply older than what is on
 * screen. So a load compares this counter across its own await and throws away a page
 * that is answering a question about a list that has since changed.
 *
 * MEMO-07 left this open and named this task as the fix, on the reading that replacing
 * by id would close it. It does not, and the two mechanisms are worth telling apart:
 * keying by id is what makes a tick write only what changed, and this counter is what
 * makes a tick never write something stale. Discarding costs one interval of freshness
 * for the other rows, in a window that is the duration of one in-flight GET.
 */
let revision = 0

/**
 * The box's contents as the API wants them: trimmed, and null rather than empty.
 *
 * Trimmed here and again by ListMemosRequest, which is agreement rather than reliance --
 * without the client-side half, a trailing space from a paste would be sent as part of the
 * filter and the ILIKE pattern would be `%dentist %`.
 *
 * `!== ''` rather than a truthiness test: '0' is falsy and is a perfectly good thing to
 * search for. The API's own accessor makes the same point, and both are pinned by a test.
 */
function activeQuery() {
  const trimmed = query.value.trim()

  return trimmed === '' ? null : trimmed
}

/**
 * GET the page and merge it in, keyed by id.
 *
 * @param {{background?: boolean}} [options] `background: true` for a poll tick: the
 *   fetch is identical, only the reporting differs. See `loading`.
 */
async function load({ background = false } = {}) {
  if (inFlight) {
    // Deferred rather than dropped, but only for a load somebody is waiting on. Dropping
    // a duplicate Refresh costs nothing and dropping a poll tick costs one interval --
    // usePolling schedules another -- but dropping a *search* leaves the list showing
    // results for a query the user has already changed, with nothing further coming to
    // correct it. So the last interactive request always happens: at most one GET is in
    // flight, and whatever was asked for meanwhile runs the moment it lands.
    if (!background) {
      reloadWanted = true
    }

    return
  }

  inFlight = true

  if (!background) {
    loading.value = true
  }

  const revisionAtRequest = revision

  try {
    // One fetch between the poll, the button and the filter, which is what MEMO-18 set
    // this up for: the tick reads the same `query` the user is typing into, so it polls
    // the filtered page with no wiring of its own and no second copy of the query to
    // drift. The one visible consequence is that a tick landing inside the debounce
    // applies a half-typed filter up to 250ms early -- reachable only while something is
    // transcribing, and it shows the user results for what they are in the middle of
    // typing, which is the direction they were going anyway.
    const page = await listMemos(activeQuery())

    if (revision === revisionAtRequest) {
      replacePage(page.memos)

      // Inside the same guard as the rows, so the caption and the rows it describes can
      // never come from different responses.
      appliedQuery.value = page.query
    }

    loadError.value = null
  } catch (error) {
    loadError.value = `Could not load memos — ${error.message}`
  } finally {
    inFlight = false
    loading.value = false

    if (reloadWanted) {
      reloadWanted = false

      // Not awaited, and it must not be: this is inside the finally of the load that just
      // finished, so awaiting would keep that call on the stack for as long as the chain
      // of follow-ups lasts. Interactive by default, which is what a deferred search is.
      load()
    }
  }
}

/**
 * Type into the filter. Debounced, so holding a key down is one request at the end rather
 * than one per repeat.
 *
 * @param {string} next
 */
function search(next) {
  query.value = next

  clearTimeout(debounce)

  // Set before the timer, not inside it: the whole point is to cover the wait.
  debouncing.value = true

  debounce = setTimeout(() => {
    debounce = null
    debouncing.value = false
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

  // The wait is over rather than cancelled -- load() takes over from here, and leaving
  // this set would strand `busy` true for good.
  debouncing.value = false

  load()
}

/** Empty the box and show everything again. */
function clearSearch() {
  query.value = ''

  searchNow()
}

/**
 * Take the response as the page -- its order and its membership -- while keeping the
 * object already held for each id.
 *
 * This is the "replace by id" half of MEMO-18, and what it is not is worth saying,
 * because Vue already handles the obvious thing: `v-for` is keyed by `memo.id` in
 * MemoList.vue, so DOM nodes are reused across a wholesale replacement regardless.
 * Element churn is not what this rescues, and neither is anything living on those nodes
 * -- an <audio> element mid-playback (MEMO-23) survives either way, because its <li> is
 * never recreated.
 *
 * What it buys is that a tick which finds nothing new triggers nothing. Assigning a
 * fresh array of fresh objects moves the array reference and rewrites every field of
 * every row, so the render effect re-runs and rebuilds fifty vnodes in order to
 * conclude that the DOM is already correct -- every 2 seconds, for a page that is
 * usually identical to the one on screen. Here neither happens: the reference moves
 * only when membership or order does, no field is written unless it differs, and a tick
 * that brings no news invalidates not one dependency.
 *
 * A tick that does bring news still re-renders the whole list once, because MemoList is
 * a single render effect rather than fifty components -- so against a wholesale replace
 * this saves nothing on the tick that matters. The whole win is in the common case, and
 * the common case is nearly every tick.
 *
 * @param {Array<object>} page
 */
function replacePage(page) {
  const held = new Map(memos.value.map((memo) => [memo.id, memo]))

  let pageChanged = page.length !== memos.value.length

  const merged = page.map((row, position) => {
    const existing = held.get(row.id)

    if (existing === undefined) {
      pageChanged = true

      return row
    }

    // Same rows, different order: the array has to move even though every object in
    // it is one we already had. Not reachable today -- the API orders by created_at
    // DESC and nothing edits that column -- and checked anyway, because the cost is
    // one comparison and the failure would be a list rendering in stale order with no
    // way to tell from the screen that it had.
    if (memos.value[position] !== existing) {
      pageChanged = true
    }

    for (const [field, value] of Object.entries(row)) {
      if (!unchanged(existing[field], value)) {
        existing[field] = value
      }
    }

    return existing
  })

  if (pageChanged) {
    memos.value = merged
  }
}

/**
 * Whether writing `next` over `current` would be a no-op.
 *
 * The array branch exists for exactly one field, and it is load-bearing rather than a
 * refinement. `tags` arrives as a fresh array in every response, so `===` calls it
 * changed on every tick even when the memo has carried the same three tags for a week.
 * MemoList reads it on every row -- `v-if="memo.tags?.length"` -- so writing it is a
 * dependency invalidation on every row, and without this branch the "tick that brings
 * no news" above would re-render the entire list every 2 seconds, which is the one
 * thing replacePage exists to avoid.
 *
 * Shallow is enough: the elements are strings. It costs nothing today either -- MEMO-21
 * is what first puts a tag on a row, so every array compared here is currently empty.
 */
function unchanged(current, next) {
  if (Array.isArray(current) && Array.isArray(next)) {
    return current.length === next.length && current.every((item, at) => item === next[at])
  }

  return current === next
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
 * The row arrives `queued`, which flips `pending` and is what starts the poll. Nothing
 * here talks to the timer.
 *
 * Prepending is also what keeps the new memo on screen while a filter is active, and it
 * agrees with what the API would answer rather than working around it: a memo that has not
 * been enriched yet is pinned into every filtered page regardless of match
 * (MemoRepository::search), so the row this puts at the top is the row the next tick brings
 * back. Once the worker finishes with it, a memo that does not match the filter drops out
 * -- which is the filter working, not the memo being lost.
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

    // Synchronous with the line above, and it has to be: anything between the two
    // could be an in-flight GET resolving into the gap.
    revision += 1

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
    pending,
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

import { computed, ref } from 'vue'
import { createMemo, listMemos } from '../api/memos'

/*
 * The memo list, and the three things that change it.
 *
 * The state is declared at module scope rather than inside useMemos(), which is what
 * makes this one shared array instead of a fresh one per calling component: the
 * composer needs to prepend to the same list MemoList renders, and it should not have
 * to reach it through props and events routed via App.vue.
 *
 * No Pinia. A store would buy devtools time-travel and module namespacing for an app
 * whose entire state is the five refs below, and it fails the same over-engineering
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
 * condition -- see App.vue, which hands it to usePolling().
 *
 * "On screen" is meant literally: it reads the page the API returned, so a memo past
 * the limit, or filtered out by MEMO-19's search, is not something this waits for.
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
 * GET the page and merge it in, keyed by id.
 *
 * @param {{background?: boolean}} [options] `background: true` for a poll tick: the
 *   fetch is identical, only the reporting differs. See `loading`.
 */
async function load({ background = false } = {}) {
  if (inFlight) {
    return
  }

  inFlight = true

  if (!background) {
    loading.value = true
  }

  const revisionAtRequest = revision

  try {
    // listMemos() and nothing else, which is what the poll and the button having one
    // fetch between them actually buys. MEMO-19 adds `q` to this call, and because the
    // tick goes through the same function it starts polling the filtered page on that
    // day with no change here: there is no second place holding a copy of the query
    // that could drift from the one the user is looking at.
    const page = await listMemos()

    if (revision === revisionAtRequest) {
      replacePage(page)
    }

    loadError.value = null
  } catch (error) {
    loadError.value = `Could not load memos — ${error.message}`
  } finally {
    inFlight = false
    loading.value = false
  }
}

/**
 * Take the response as the page -- its order and its membership -- while keeping the
 * object already held for each id.
 *
 * This is the "replace by id" half of MEMO-18, and what it is not is worth saying,
 * because Vue already handles the obvious thing: `v-for` is keyed by `memo.id` in
 * MemoList.vue, so DOM nodes are reused across a wholesale replacement regardless and
 * this buys no rescue from element churn.
 *
 * What it buys is that a tick which changes nothing writes nothing. Assigning a fresh
 * array of fresh objects every 2 seconds invalidates every row's reactive dependencies
 * and re-runs the render for all of them, forever, for a list that is usually
 * identical to the one already there. Here the array reference only moves when
 * membership or order does, and a row is only touched on the fields that differ -- so
 * the steady state of waiting on one memo among fifty is fifty untouched rows and one
 * status string. It is also what will let in-row state survive a tick once there is
 * any: MEMO-23's audio element is the case, mid-playback, every 2 seconds.
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
 * The array branch exists for exactly one field. `tags` arrives as a fresh array in
 * every response, so `===` reports it as changed on every tick even when the memo has
 * carried the same three tags for a week -- and MEMO-21 is what puts tags on rows at
 * all. Shallow is enough: the elements are strings.
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
  return { memos, pending, loading, saving, loadError, saveError, load, submit }
}

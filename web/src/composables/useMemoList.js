import { computed, ref } from 'vue'
import { listMemos } from '../api/memos'
import { useDateRange } from './useDateRange'

/*
 * One filtered, pollable list of memos -- as a factory, not as shared state.
 *
 * **Why this is a factory when useMemos was a singleton.** Until now there was one list on
 * screen and module-scoped refs were the right shape for it: the composer had to prepend
 * into the same array MemoList rendered, without routing props through App.vue. There are
 * two lists now -- the fast strip, and the memos inside whichever collection has been opened
 * -- and they filter independently: typing in the strip's search box must not re-filter the
 * open collection, and the collection's own date range is its own. Shared refs cannot
 * express that, so the machinery moved here and the state is created per caller.
 *
 * useMemos.js keeps exactly one long-lived instance of this -- the fast strip -- because
 * that one *is* shared: it is where a newly recorded memo lands, and the recorder should not
 * have to be handed it. Everything else creates its own and throws it away.
 *
 * Still no Pinia. The reason in useMemos.js has not changed and this makes it stronger
 * rather than weaker: what was missing was never a store, it was a way to have more than one
 * of something, and a factory is that.
 */

/**
 * The two statuses nothing further happens to. 001_init.sql allows four; the other two
 * are `queued` and `processing`.
 *
 * Stated as the terminal set and negated, rather than as the non-terminal set and
 * tested directly, and the direction is the whole point of MEMO-18. The design this
 * replaced polled "while any memo is queued" -- a positive list, one value short. The
 * worker's first act is queued -> processing, so that poll stopped within a tick and the
 * browser never heard about the transcript. Any positive list has the same failure
 * available to it the moment a status is added: MEMO-16 introduces a retry path, and a
 * status this file has never heard of must keep the page live rather than freeze it.
 * Unknown means not finished.
 */
const TERMINAL_STATUSES = new Set(['ready', 'failed'])

/**
 * Long enough that ordinary typing produces one request instead of one per character,
 * short enough that the list feels attached to the box. The API is same-origin through the
 * dev proxy, so there is no round trip worth hiding behind a longer wait.
 */
const DEBOUNCE_MS = 250

/**
 * Every list currently on screen.
 *
 * **This exists because "bring that memo up to date" turned out to be a question about all of
 * them, and answering it for one produced the same bug twice.** The write path knew only about
 * the fast strip, so a memo opened from inside a collection did not update when its reminder
 * was set -- silently, since the request succeeded. The delivery loop had the mirror of it: a
 * reminder firing marked itself delivered on the server while the card behind it went on
 * showing the alarm badge. Two symptoms, one cause.
 *
 * A Set rather than an array so `dispose` is a delete rather than a search, and module-scoped
 * because the whole point is that no caller has to know which lists exist.
 *
 * @type {Set<object>}
 */
const lists = new Set()

/**
 * Write a memo the API just returned into every list holding it.
 *
 * Lists that do not hold it ignore it, so this is safe to call after any write without knowing
 * where the memo was being rendered -- which is the property that makes the bug above
 * unrepeatable rather than merely fixed.
 *
 * @param {object} updated
 */
export function applyMemoEverywhere(updated) {
  for (const list of lists) {
    list.applyUpdate(updated)
  }
}
/**
 * Build one list.
 *
 * @param {{collection?: ?string}} [options] `collection` is fixed for the life of the list
 *   and is the one filter the user cannot change from inside it: 'none' for the fast strip,
 *   a collection id for an opened collection, null for everything. It is a constructor
 *   argument rather than a ref precisely because it identifies *which list this is* -- a
 *   strip that could be re-scoped would be a different list wearing the same state.
 */
export function createMemoList({ collection = null } = {}) {
  /**
   * Newest first, which is the order the API returns and the order prepend() maintains.
   *
   * @type {import('vue').Ref<Array<object>>}
   */
  const memos = ref([])

  /**
   * Whether any memo on screen is still going to change. This is the poll's entire stop
   * condition, and the search status line reads it too -- to say out loud that a row not
   * containing the filter text is there on purpose.
   *
   * "On screen" is meant literally: it reads the page the API returned, so a memo past the
   * limit is not something this waits for.
   *
   * A filter cannot strand the poll, because a non-terminal memo is pinned into every
   * filtered page regardless of match (MemoRepository::list). That pin is now bounded by
   * the date window and the collection scope, which does not change anything here: a memo
   * outside this list's scope was never this list's to wait for.
   */
  const pending = computed(() => memos.value.some((memo) => !TERMINAL_STATUSES.has(memo.status)))

  /**
   * Only true for a load somebody asked for -- the first one and the Refresh button.
   *
   * A poll tick deliberately leaves it alone. It is read by the button's label and by the
   * "Loading…" placeholder, and both are answers to "you asked for this, it is happening";
   * a timer firing every 2 seconds is not a question anybody asked. Wired to the tick it
   * would flicker the button through "Refreshing…" and grey it out on every interval, for
   * the few milliseconds a GET takes against localhost.
   */
  const loading = ref(false)

  const loadError = ref(null)

  /**
   * What is in this list's search box, and the only thing besides the date range that
   * decides what the next GET asks for -- including a poll tick's, which is what keeps the
   * timer on the page the user is actually looking at.
   *
   * @type {import('vue').Ref<string>}
   */
  const query = ref('')

  /** This list's own date filter. Independent per list -- see the header. */
  const dateRange = useDateRange()

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
   * Not exposed as two separate facts, because both places that describe a filtered list --
   * the status line and the empty state -- have to agree about when one is in effect, and
   * while MEMO-19 was being built they did not. One derived value, read by both, is what
   * stops that drifting again.
   *
   * The box decides *whether* a filter is in effect and the API's echo decides *what to
   * call it*. Each is authoritative for its half: the box is ahead of the network, so it is
   * the only thing that knows the filter has been dropped, while the rows on screen belong
   * to whatever the API last answered -- naming them after the box would caption them with
   * a query they were not the answer to.
   *
   * @type {import('vue').ComputedRef<?string>}
   */
  const displayedFilter = computed(() => (query.value.trim() === '' ? null : appliedQuery.value))

  /**
   * Whether a filter change has been typed but its request has not started yet.
   *
   * Named for the wait rather than for the work, because `pending` above is taken and means
   * something else entirely -- that the *server* still owes a memo something. This is the
   * client waiting on its own timer.
   *
   * It exists because `loading` leaves a hole exactly the width of the debounce, and the
   * list decides what to say about an empty result by asking whether a fresher one is
   * coming. In that window nothing is in flight and the list is already out of date, so an
   * empty list gets described with certainty from a request that has not happened:
   * backspace the last character of a filter that matched nothing and the page states, for
   * a quarter of a second, that there are no memos at all. Measured before the fix, on a
   * database holding five.
   */
  const debouncing = ref(false)

  /**
   * "The list on screen is not the answer to the current filter, and something is on its
   * way." True across both halves of that: the debounce and the request.
   *
   * Kept separate from `loading` rather than folded into it, because the two answer
   * different questions -- and `loading` has already been narrowed once, by MEMO-18, so
   * that a poll tick does not flicker the Refresh button. Widening it here would undo that.
   */
  const busy = computed(() => loading.value || debouncing.value)

  /** The pending debounce timer, or null when nothing is scheduled. */
  let debounce = null

  /** Set when an interactive load is dropped by the in-flight guard. See load(). */
  let reloadWanted = false

  /**
   * Guards the GET against overlapping with itself. Not a ref, because nothing renders it:
   * `loading` used to serve both purposes and cannot, now that a background tick has to
   * take the guard without touching the label.
   */
  let inFlight = false

  /**
   * Counts writes this client has already applied to `memos`.
   *
   * The problem it solves is ordering, not identity, and it is the half that replacing the
   * page by id does not cover. A GET issued before a POST lands describes a list the new
   * memo was not in yet; merging that response by id still drops the row, because
   * membership comes from the response and the response is simply older than what is on
   * screen. So a load compares this counter across its own await and throws away a page
   * that is answering a question about a list that has since changed.
   *
   * Keying by id is what makes a tick write only what changed; this counter is what makes a
   * tick never write something stale. Discarding costs one interval of freshness for the
   * other rows, in a window that is the duration of one in-flight GET.
   */
  let revision = 0

  /**
   * The box's contents as the API wants them: trimmed, and null rather than empty.
   *
   * Trimmed here and again by ListMemosRequest, which is agreement rather than reliance --
   * without the client-side half, a trailing space from a paste would be sent as part of
   * the filter and the ILIKE pattern would be `%dentist %`.
   *
   * `!== ''` rather than a truthiness test: '0' is falsy and is a perfectly good thing to
   * search for. The API's own accessor makes the same point, and both are pinned by a test.
   */
  function activeQuery() {
    const trimmed = query.value.trim()

    return trimmed === '' ? null : trimmed
  }

  /** Everything the next GET should be narrowed by. */
  function activeFilter() {
    return {
      query: activeQuery(),
      from: dateRange.from,
      to: dateRange.to,
      collection,
    }
  }

  /**
   * GET the page and merge it in, keyed by id.
   *
   * @param {{background?: boolean}} [options] `background: true` for a poll tick: the fetch
   *   is identical, only the reporting differs. See `loading`.
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
      // One fetch between the poll, the button, the search box and the date filter: the
      // tick reads the same state the user is editing, so it polls the filtered page with
      // no wiring of its own and no second copy of the filter to drift. The one visible
      // consequence is that a tick landing inside the debounce applies a half-typed filter
      // up to 250ms early -- reachable only while something is transcribing, and it shows
      // results for what the user is in the middle of typing, which is the direction they
      // were going anyway.
      const page = await listMemos(activeFilter())

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
   * Filter now, without waiting out the debounce -- what Enter, the clear button and the
   * date presets do.
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

  /** Empty the box and show everything again. The date filter is left alone. */
  function clearSearch() {
    query.value = ''

    searchNow()
  }

  /**
   * Change the date filter and reload immediately.
   *
   * Not debounced, unlike typing: a preset is one click rather than a stream of keystrokes,
   * and a custom range only applies once both ends are valid. Waiting 250ms after a click
   * would just be a delay.
   *
   * @param {...unknown} args Forwarded to useDateRange's own setter.
   */
  function applyDateRange(...args) {
    dateRange.set(...args)

    searchNow()
  }

  /**
   * Take the response as the page -- its order and its membership -- while keeping the
   * object already held for each id.
   *
   * This is the "replace by id" half of MEMO-18, and what it is not is worth saying,
   * because Vue already handles the obvious thing: `v-for` is keyed by `memo.id`, so DOM
   * nodes are reused across a wholesale replacement regardless. Element churn is not what
   * this rescues, and neither is anything living on those nodes -- an <audio> element
   * mid-playback (MEMO-23) survives either way, because its element is never recreated.
   *
   * What it buys is that a tick which finds nothing new triggers nothing. Assigning a fresh
   * array of fresh objects moves the array reference and rewrites every field of every row,
   * so the render effect re-runs and rebuilds fifty vnodes in order to conclude that the
   * DOM is already correct -- every 2 seconds, for a page that is usually identical to the
   * one on screen. Here neither happens: the reference moves only when membership or order
   * does, no field is written unless it differs, and a tick that brings no news invalidates
   * not one dependency.
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

      // Same rows, different order: the array has to move even though every object in it is
      // one we already had. Not reachable today -- the API orders by created_at DESC and
      // nothing edits that column -- and checked anyway, because the cost is one comparison
      // and the failure would be a list rendering in stale order with no way to tell from
      // the screen that it had.
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
   * Put a memo at the top, and count the write.
   *
   * Used by the write path in useMemos: a newly created memo is unfiled, so it belongs at
   * the top of the fast strip, and the row the API returned is the row the next poll brings
   * back.
   *
   * The revision bump has to be synchronous with the array assignment -- anything between
   * the two could be an in-flight GET resolving into the gap and replacing the page with
   * one this memo was not in.
   *
   * @param {object} memo
   */
  function prepend(memo) {
    memos.value = [memo, ...memos.value]
    revision += 1
  }

  /**
   * Write a memo the API just returned over the copy this list is holding, if it holds one.
   *
   * Every write route -- filing, reminders -- answers with the whole memo precisely so this
   * can be a field-by-field write rather than a re-fetch. A list that does not hold that id
   * ignores it, which is what makes it safe to call on a list without first knowing whether
   * the memo is in it.
   *
   * **It is not enough on its own, and that is worth knowing before relying on it.** Only the
   * fast strip is handed updates this way (useMemos owns that one instance). A memo opened
   * from inside a collection belongs to a different list, so the write path also merges into
   * the object the caller is rendering -- see mergeMemo, and writeMemo for the bug that
   * omission caused.
   *
   * @param {object} updated
   * @returns {boolean} Whether this list held the memo. Nothing reads it today; it is here
   *   because "did that land anywhere?" is the one question a caller might reasonably have of
   *   a method that silently does nothing.
   */
  function applyUpdate(updated) {
    const existing = memos.value.find((memo) => memo.id === updated.id)

    if (existing === undefined) {
      return false
    }

    mergeMemo(existing, updated)

    return true
  }

  const api = {
    memos,
    pending,
    loading,
    busy,
    loadError,
    query,

    // `appliedQuery` itself is deliberately not returned. It is the raw echo, and a
    // component reading it directly is the bug displayedFilter exists to prevent.
    displayedFilter,

    dateRange,
    collection,

    load,
    search,
    searchNow,
    clearSearch,
    applyDateRange,
    prepend,
    applyUpdate,

    /**
     * Stop this list receiving updates.
     *
     * Only the collection dialog needs it, and it genuinely needs it: it builds a fresh list
     * every time a collection is opened, so without this the registry would grow by one for
     * every open and every discarded list would go on being written to forever.
     *
     * The fast strip never calls it -- it lives as long as the app does.
     */
    dispose: () => lists.delete(api),
  }

  lists.add(api)

  return api
}

/**
 * Write a memo the API returned over the copy a list is already holding, field by field.
 *
 * Field-by-field rather than replacing the object, and that is the whole point: the object in
 * the list is the same one an open detail card is rendering through its `memo` prop.
 * Reassigning would update the list's slot and leave the card pointing at the old object.
 *
 * Module-private. It was briefly exported, when the fix for that bug was "have each caller
 * merge into whatever it is rendering" -- which worked and put the responsibility in the wrong
 * place. `applyMemoEverywhere` replaced it: callers no longer need to know which list holds
 * the memo, so nothing outside this file needs to merge.
 *
 * @param {object} target The object to bring up to date, mutated in place.
 * @param {object} source The memo as the API just returned it.
 */
function mergeMemo(target, source) {
  for (const [field, value] of Object.entries(source)) {
    if (!unchanged(target[field], value)) {
      target[field] = value
    }
  }
}

/**
 * Whether writing `next` over `current` would be a no-op.
 *
 * The array branch is load-bearing rather than a refinement. `tags` arrives as a fresh array
 * in every response, so `===` calls it changed on every tick even when the memo has carried
 * the same three tags for a week -- and `reminders` is now a fresh array of fresh *objects*
 * on every response, which is worse: it is never `===`, so without a check every memo would
 * be rewritten on every poll and the whole list would re-render every 2 seconds, which is
 * the one thing replacePage exists to avoid.
 *
 * Shallow equality is not enough for reminders, since the elements are objects rather than
 * strings. Rather than a deep compare, they are compared by their JSON -- which is exact
 * here because the shape is flat, fixed and ordered by the SQL that produced it, so two
 * arrays with the same content always serialise identically. A general deep-equal would be
 * more code for a case that cannot occur.
 *
 * Module scope rather than inside the factory: it closes over nothing, and one function is
 * better than one per list.
 */
function unchanged(current, next) {
  if (Array.isArray(current) && Array.isArray(next)) {
    if (current.length !== next.length) {
      return false
    }

    return current.every((item, at) =>
      item !== null && typeof item === 'object'
        ? JSON.stringify(item) === JSON.stringify(next[at])
        : item === next[at],
    )
  }

  return current === next
}

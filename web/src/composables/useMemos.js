import { ref } from 'vue'
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
 * GET the list and replace it wholesale.
 *
 * The in-flight guard is not about load: it is about two responses. Overlapping GETs
 * resolve in whatever order the network decides, so the last one to arrive wins --
 * which can be the older answer. That is reachable today by double-clicking Refresh,
 * and MEMO-18 turns this into a timer where it would be routine.
 */
async function load() {
  if (loading.value) {
    return
  }

  loading.value = true

  try {
    memos.value = await listMemos()
    loadError.value = null
  } catch (error) {
    loadError.value = `Could not load memos — ${error.message}`
  } finally {
    loading.value = false
  }
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
  return { memos, loading, saving, loadError, saveError, load, submit }
}

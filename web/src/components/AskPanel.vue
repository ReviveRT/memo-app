<script setup>
import { computed, onScopeDispose, ref } from 'vue'
import { askMemos } from '../api/ask'
import { memoLabel } from '../memoLabel'

/*
 * Ask a question about the memos, and watch the answer being written (MEMO-24).
 *
 * **The sources appear before the answer does, and that is the design rather than a loading
 * state.** Retrieval is one Postgres query and lands in milliseconds; the answer is a local
 * model on a CPU and takes seconds. Showing the memos as soon as they are known means the wait
 * is spent reading the evidence rather than watching a spinner -- and on the questions where
 * the retrieved memos are obviously wrong, it is also the fastest way to find out that
 * rephrasing would be quicker than waiting.
 *
 * It owns its own state rather than using a composable, unlike every list on this screen. The
 * difference is real: a list is a shared singleton that several components read and a poll
 * writes to, and this is one question with one answer belonging to one panel. There is nothing
 * for a second reader to see.
 */

const emit = defineEmits(['open-memo'])

const props = defineProps({
  /**
   * The memos currently on screen, so a citation can open the card rather than only name it.
   *
   * The list rather than a lookup function, because the strip already holds these objects and
   * MemoDialog wants the *same* object -- MemosView's `showMemo` has the argument for why a
   * copy would stop updating when a poll rewrites the row.
   *
   * Cited memos that are not in it are shown without the link. That is an ordinary case rather
   * than an edge one: ask reads every memo in the table, and this strip holds only the unfiled
   * ones under whatever filter is active.
   */
  memos: { type: Array, required: true },
})

const question = ref('')

/** The memos the answer is being built from, `ref`-numbered by the API. */
const sources = ref([])

/** The answer so far. Appended to on every token, which is what makes it stream. */
const answer = ref('')

/** The refs the finished answer actually cited, or null while it is still being written. */
const cited = ref(null)

const error = ref('')

/**
 * The in-flight request's controller, or null.
 *
 * Doubles as the "is it running" flag rather than a second ref beside it, so the two cannot
 * disagree -- a Stop button enabled with nothing to stop is the failure that pairing invites.
 */
const running = ref(null)

const busy = computed(() => running.value !== null)

/** Whether there is anything on screen worth keeping while a new question is asked. */
const answered = computed(() => sources.value.length > 0 || answer.value !== '')

/**
 * The sources, in the order the answer referred to them, once it is finished.
 *
 * Uncited memos are dropped rather than greyed out. They were retrieved because they matched
 * some word of the question, which is not the same as having contributed to the answer -- and
 * a list mixing "this is where that came from" with "this happened to match" makes the first
 * claim untrustworthy. While the answer is still being written `cited` is null and all of them
 * are shown, because that is honestly what is known at that moment.
 */
const shown = computed(() => {
  if (cited.value === null) {
    return sources.value
  }

  return cited.value
    .map((ref_) => sources.value.find((source) => source.ref === ref_))
    .filter(Boolean)
})

/** The memo object behind a citation, if this screen is holding it. */
function memoFor(source) {
  return props.memos.find((memo) => memo.id === source.id) ?? null
}

/** What to call a cited memo: its own title, or the label the rest of the app derives. */
function label(source) {
  const memo = memoFor(source)

  return memo ? memoLabel(memo) : (source.title ?? 'Untitled memo')
}

async function ask() {
  const asked = question.value.trim()

  if (asked === '' || busy.value) {
    return
  }

  const controller = new AbortController()

  running.value = controller
  error.value = ''
  answer.value = ''
  cited.value = null
  sources.value = []

  try {
    await askMemos(asked, {
      onSources: (found) => (sources.value = found),
      onToken: (text) => (answer.value += text),
      onDone: (refs) => (cited.value = refs),
      signal: controller.signal,
    })
  } catch (failure) {
    // An abort is the user pressing Stop, not a failure. Whatever had arrived stays on screen,
    // which is the point of stopping rather than cancelling.
    if (failure.name !== 'AbortError') {
      error.value = failure.message
    }
  } finally {
    // Only if this request is still the current one. A second question started while the first
    // was finishing would otherwise have its controller cleared by the first one's cleanup, and
    // the Stop button would go dead with a request still running.
    if (running.value === controller) {
      running.value = null
    }
  }
}

function stop() {
  running.value?.abort()
}

/*
 * Aborting on unmount rather than leaving it to the browser.
 *
 * Navigating to the landing page mid-answer would otherwise leave the model generating into a
 * response nobody will read -- and unlike a dropped fetch, that costs real CPU on the machine
 * this is running on until ai-api notices. onScopeDispose for the reason MemosView gives about
 * its own: what this belongs to is the setup scope.
 */
onScopeDispose(() => running.value?.abort())
</script>

<template>
  <section class="section ask" aria-labelledby="ask-heading">
    <header class="section__head">
      <h2 id="ask-heading">Ask your memos</h2>
    </header>

    <p class="section__hint">
      A question in your own words. A local model reads the few memos that match and answers
      from them — nothing leaves this machine.
    </p>

    <!--
      A <form>, so Enter submits without a keydown handler -- the same reason MemoComposer and
      ListFilters use one. submit.prevent because the default is a page reload.
    -->
    <form class="ask__form" @submit.prevent="ask">
      <input
        v-model="question"
        class="filters__input"
        type="text"
        name="question"
        placeholder="What did I say about the landing page?"
        aria-label="Ask a question about your memos"
        autocomplete="off"
        maxlength="500"
      />

      <!--
        Two buttons in one slot rather than one that changes meaning. A control whose label
        flips between Ask and Stop is one somebody presses expecting the other, and the two do
        opposite things.
      -->
      <button v-if="!busy" type="submit" :disabled="question.trim() === ''">Ask</button>

      <button v-else type="button" class="filters__clear" @click="stop">Stop</button>
    </form>

    <!--
      `busy` is in the condition as well as `answered`, and it is not redundant: the sources
      take a Postgres query to arrive and the answer takes seconds, so without it the panel
      shows nothing at all between the press and the first event. On a loaded machine that gap
      was two seconds of a page that looked like the button had done nothing.
    -->
    <div v-if="busy || answered || error" class="ask__result">
      <!--
        aria-live, because the answer arrives after the press rather than because of it, and a
        screen reader would otherwise announce nothing at all. `polite` and not `assertive`: it
        is an answer somebody asked for, not an alert.

        aria-busy says the text is still being written, which is what stops a reader being
        given a half sentence as though it were the whole answer.
      -->
      <p
        v-if="answer !== ''"
        class="ask__answer"
        aria-live="polite"
        :aria-busy="busy"
      >
        {{ answer }}<span v-if="busy" class="ask__cursor" aria-hidden="true"></span>
      </p>

      <!--
        Shown while the model is still working, which is the wait this panel exists to fill.
        Dropped as soon as the first token lands.

        Two sentences rather than one, because the two waits are different lengths and the
        reader can tell them apart: the search is a Postgres query and is over in milliseconds,
        and what follows is a local model reading. Saying "Reading 0 memos…" for the first of
        them would be both wrong and the more alarming of the two.
      -->
      <p v-else-if="busy" class="ask__waiting" role="status">
        <template v-if="sources.length === 0">Looking for memos that match…</template>
        <template v-else>
          Reading {{ sources.length }} {{ sources.length === 1 ? 'memo' : 'memos' }}…
        </template>
      </p>

      <p v-if="error" class="notice notice--error" role="alert">{{ error }}</p>

      <!--
        The evidence. Every entry is a memo the answer was actually built from, and the excerpt
        is the same text the model was shown -- so checking a citation is checking what was
        read, not a different rendering of the memo.
      -->
      <ul v-if="shown.length > 0" class="ask__sources">
        <li v-for="source in shown" :key="source.id" class="ask__source">
          <span class="ask__ref" aria-hidden="true">[{{ source.ref }}]</span>

          <div class="ask__source-body">
            <!--
              A button rather than a link, because it opens a dialog rather than navigating.
              Only when this screen is holding the memo -- ask reads the whole table and the
              strip holds the unfiled ones, so a cited memo can be one filed away in a
              collection. It is still named; it just cannot be opened from here.
            -->
            <button
              v-if="memoFor(source)"
              type="button"
              class="ask__open"
              @click="emit('open-memo', memoFor(source))"
            >
              {{ label(source) }}
            </button>

            <span v-else class="ask__title">{{ label(source) }}</span>

            <p class="ask__excerpt">
              {{ source.excerpt }}<template v-if="source.truncated"> (excerpt)</template>
            </p>
          </div>
        </li>
      </ul>
    </div>
  </section>
</template>

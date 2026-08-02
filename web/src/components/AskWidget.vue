<script setup>
import { computed, nextTick, onScopeDispose, ref, useTemplateRef, watch } from 'vue'
import AskOrb from './AskOrb.vue'
import { askMemos } from '../api/ask'
import { getMemo } from '../api/memos'
import { memoLabel } from '../memoLabel'

/*
 * Ask a question about the memos, from a sphere in the corner of the screen (MEMO-24).
 *
 * This was a section in the page, between the composer and the fast strip, and the change is
 * about what the feature *is* rather than about where it looks best. As a section it took a
 * band of the page permanently to show an empty box, it pushed the memo list down by the height
 * of its own answer whenever one arrived, and it could only be reached by scrolling back to it.
 * As a widget it costs a corner, it is reachable from anywhere on the page, and an answer
 * opening does not move the list somebody is reading.
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
 * writes to, and this is one question with one answer belonging to one widget. There is nothing
 * for a second reader to see.
 */

const emit = defineEmits(['open-memo'])

const props = defineProps({
  /**
   * The memos currently on screen, so a citation can open the card the strip is already
   * holding rather than fetching a second copy of it.
   *
   * The list rather than a lookup function, because the strip already holds these objects and
   * MemoDialog wants the *same* object -- MemosView's `showMemo` has the argument for why a
   * copy would stop updating when a poll rewrites the row.
   *
   * A cited memo that is not in it is fetched instead (see `openSource`), so this is now an
   * optimisation rather than the thing that decides whether a citation opens at all.
   */
  memos: { type: Array, required: true },
})

/** Whether the panel is showing. The orb is always there. */
const open = ref(false)

const question = ref('')

/**
 * The question the thing on screen is the answer to.
 *
 * Separate from `question`, which is whatever is in the box: the box keeps its text so it can
 * be edited into a follow-up, and the answer has to keep saying which question it answers. They
 * are the same string until somebody starts typing, and then they are not -- which is exactly
 * the moment a reader needs the answer to still be labelled.
 */
const asked = ref('')

/** The memos the answer is being built from, `ref`-numbered by the API. */
const sources = ref([])

/** The answer so far. Appended to on every token, which is what makes it stream. */
const answer = ref('')

/** The refs the finished answer actually cited, or null while it is still being written. */
const cited = ref(null)

const error = ref('')

/** A citation that could not be opened, which is a different failure from the one above. */
const openError = ref('')

/** The id of the cited memo currently being fetched, or null. */
const opening = ref(null)

/**
 * Whether the answer on screen was cut short by the Stop button.
 *
 * **Without it a stopped answer is indistinguishable from a finished one**, which is the same
 * failure `askMemos` guards against for a truncated stream and it arrives by a friendlier
 * route: the caret disappears, the button says Ask again, and a sentence that stopped mid-
 * clause looks like all the model had to say. An abort is not an error -- it is what the
 * reader asked for -- so it gets a note rather than the error box.
 */
const stopped = ref(false)

/**
 * The in-flight request's controller, or null.
 *
 * Doubles as the "is it running" flag rather than a second ref beside it, so the two cannot
 * disagree -- a Stop button enabled with nothing to stop is the failure that pairing invites.
 */
const running = ref(null)

const inputEl = useTemplateRef('inputEl')
const orbEl = useTemplateRef('orbEl')
const resultEl = useTemplateRef('resultEl')

const busy = computed(() => running.value !== null)

/** Whether there is anything on screen worth keeping while a new question is asked. */
const answered = computed(() => sources.value.length > 0 || answer.value !== '')

/**
 * The memos to list under the answer, narrowed to the cited ones once it is finished.
 *
 * Uncited memos are dropped rather than greyed out. They were retrieved because they matched
 * some word of the question, which is not the same as having contributed to the answer -- and
 * a list mixing "this is where that came from" with "this happened to match" makes the first
 * claim untrustworthy. While the answer is still being written `cited` is null and all of them
 * are shown, because that is honestly what is known at that moment.
 *
 * **An answer that cited nothing keeps the whole list**, which is the exception and not a
 * lapse in that rule. Narrowing to nothing would make three memos appear while the answer
 * streamed and then vanish at the last token -- the reader loses what was read, and the
 * disappearance reads as a bug rather than as a statement. So the fallback says "these are
 * the memos it read", which is the weaker claim and the true one. The model citing nothing is
 * rare -- every answer measured on this stack cited -- and this is what stops the rare case
 * being the ugly one.
 */
const shown = computed(() => {
  if (cited.value === null || cited.value.length === 0) {
    return sources.value
  }

  return cited.value
    .map((ref_) => sources.value.find((source) => source.ref === ref_))
    .filter(Boolean)
})

/** The memo object behind a citation, if this screen is already holding it. */
function memoFor(source) {
  return props.memos.find((memo) => memo.id === source.id) ?? null
}

/**
 * What to call a cited memo.
 *
 * Three answers, in the order of how much is known:
 *
 *   * this screen is holding the memo — `memoLabel`, so a cited memo is named exactly as the
 *     card below names it. Anything else would be two names for one memo on one screen.
 *   * it is not, but it has a title — that.
 *   * neither — the date it was recorded.
 *
 * **The last one used to read "Untitled memo", and that was worse than nothing.** It names no
 * memo, and it is reachable in an ordinary way rather than a strange one: ask reads the whole
 * table, the strip holds only unfiled memos under whatever filter is active, and a memo is
 * untitled for the seconds between being recorded and being enriched. Falling back to a
 * truncated excerpt was the other candidate and it duplicates the excerpt rendered directly
 * beneath, so the date it is -- which is also what makes `created_at` a field this payload
 * carries for a reason rather than one nothing reads.
 */
function label(source) {
  const memo = memoFor(source)

  if (memo) {
    return memoLabel(memo)
  }

  return source.title ?? recorded(source.created_at)
}

/**
 * When a memo was recorded, in the reader's own zone.
 *
 * The same shape MemoStrip uses, and the same fallback: an unparseable string is printed as it
 * arrived, because "Invalid Date" says nothing about what came over the wire. The API sends
 * RFC 3339 in UTC with a literal Z (see `_iso_z` in memo_ai/ask/service.py, which exists to
 * make that true of this payload too), so every browser parses it identically.
 */
function recorded(iso) {
  const at = new Date(iso)

  return Number.isNaN(at.getTime())
    ? String(iso ?? '')
    : `Memo from ${at.toLocaleDateString([], { month: 'short', day: 'numeric' })}`
}

/**
 * Open the memo behind a citation.
 *
 * **Two ways of getting there, and the first one is not merely the faster one.** When the strip
 * is holding this memo, that object is emitted rather than a freshly fetched copy: MemosView's
 * `showMemo` explains why the dialog has to be given the list's own object, and a second copy
 * would stop updating the moment a poll rewrote the row behind it.
 *
 * The fetch is for everything else, and "everything else" is ordinary rather than rare: ask
 * reads the whole table and the strip holds the unfiled memos matching whatever is in the
 * search box, so any cited memo that has been filed into a collection lands here. Before
 * `GET /api/memos/{id}` existed this branch could not exist either, and those citations were
 * rendered as plain text -- a link that was there or not depending on where the memo happened
 * to be filed, which is not a rule anybody could learn.
 *
 * A failure gets its own slot rather than the answer's. They are different things to be told:
 * one is "that answer did not finish", the other is "that memo is gone", and the second most
 * often means exactly that -- deleted since the answer was written.
 */
async function openSource(source) {
  const held = memoFor(source)

  if (held) {
    emit('open-memo', held)

    return
  }

  // Every citation is disabled while any one of them is being fetched, so this is the guard
  // behind that rather than the thing the reader meets. The two have to agree: disabling only
  // the one being fetched -- which is what this did -- left the others live and doing nothing
  // when pressed, which reads as the link being broken.
  if (opening.value !== null) {
    return
  }

  opening.value = source.id
  openError.value = ''

  try {
    emit('open-memo', await getMemo(source.id))
  } catch (failure) {
    openError.value = failure.message

    // **Scrolled to, because it renders at the bottom of a box that is rarely scrolled there.**
    // The message sits under the list of citations, and the reader who just pressed one is
    // usually looking at the answer above it -- so without this, a citation that cannot be
    // opened is a click that appears to do nothing at all, which is the exact impression the
    // message exists to prevent.
    nextTick(() => {
      const el = resultEl.value

      if (el) {
        el.scrollTop = el.scrollHeight
      }
    })
  } finally {
    opening.value = null
  }
}

async function ask() {
  const question_ = question.value.trim()

  if (question_ === '' || busy.value) {
    return
  }

  const controller = new AbortController()

  running.value = controller
  asked.value = question_

  // A new question starts at the top of a box that is about to be emptied, which is the
  // bottom of it as well -- so following is on until this reader scrolls away from it.
  pinned = true

  error.value = ''
  openError.value = ''
  answer.value = ''
  cited.value = null
  sources.value = []
  stopped.value = false

  try {
    await askMemos(question_, {
      onSources: (found) => (sources.value = found),
      onToken: (text) => (answer.value += text),
      onDone: (refs) => (cited.value = refs),
      signal: controller.signal,
    })
  } catch (failure) {
    // An abort is the user pressing Stop, not a failure. Whatever had arrived stays on screen
    // -- that is the point of stopping rather than cancelling -- and it is marked as cut short
    // rather than left to look like the whole answer.
    if (failure.name === 'AbortError') {
      stopped.value = true
    } else {
      error.value = failure.message
    }
  } finally {
    // Defensive rather than answering a case that exists today: `ask` returns early while
    // `busy`, so there is no second request to have its controller cleared by this one. It
    // costs a comparison and it is what keeps that true if the guard above ever loosens.
    if (running.value === controller) {
      running.value = null
    }
  }
}

function stop() {
  running.value?.abort()
}

function show() {
  open.value = true

  // The box is what somebody opened this to type in, and on a phone focusing it is also what
  // raises the keyboard. After the panel is in the DOM, hence nextTick.
  nextTick(() => inputEl.value?.focus())
}

/**
 * Close the panel, without touching what is in it.
 *
 * **The answer is kept, and the request is not aborted.** Closing is "I have read enough of
 * this for now", not "throw it away": a long answer can be left to finish while the memo it
 * cited is read, and reopening shows it complete. That is also what makes closing safe enough
 * to be one keystroke.
 *
 * Focus goes back to the orb, because that is where it came from. Without it, closing with
 * Escape drops focus onto the body and the next Tab starts from the top of the page.
 */
function hide() {
  open.value = false

  orbEl.value?.focus()
}

function toggle() {
  if (open.value) {
    hide()
  } else {
    show()
  }
}

/**
 * Whether the reader is at the bottom of the answer, and so wants to be kept there.
 *
 * A plain `let` rather than a ref: nothing renders from it, and making it reactive would put a
 * scroll event into the render pipeline for no one to read.
 *
 * **Tracked rather than worked out when it is needed, which is the correction to a version
 * that measured the box inside the watcher.** That version asked "is the box scrolled to the
 * bottom right now?" and treated yes as "follow this token" -- which is the same question only
 * while the tokens are small. An answer that arrives in one chunk goes from fitting to
 * overflowing by two hundred pixels in a single update, and the measurement then says the
 * reader is far from the bottom when what actually happened is that the bottom moved. Measured
 * live, that is not the rare case: a short answer off a warm local model lands in one or two
 * chunks, and the panel simply never scrolled.
 */
let pinned = true

/** Called by the reader's own scrolling. Scrolling up is how following is turned off. */
function trackScroll() {
  const el = resultEl.value

  if (el) {
    // Generous, so that "near enough the bottom" counts -- a reader one line short of the end
    // is still reading the end, and a strict comparison would drop the follow on a rounding
    // difference of half a pixel.
    pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }
}

/*
 * Follow the answer as it is written.
 *
 * The panel is short and an answer regularly outgrows it, so without this the text scrolls out
 * of sight within a sentence or two of the first token. Following unconditionally is the other
 * thing to avoid: somebody who has scrolled up to re-read a citation would be dragged back
 * down every few milliseconds, which is worse than not following at all -- hence `pinned`.
 *
 * `answer` alone, and not the sources with it. The sources appear all at once when the answer
 * finishes, and scrolling then would take the reader off the end of the sentence they are
 * reading and onto a list of excerpts they have not asked for yet.
 */
watch(answer, () => {
  if (!pinned) {
    return
  }

  // After the token has been rendered, or this scrolls to the height the box had before it.
  nextTick(() => {
    const el = resultEl.value

    if (el) {
      el.scrollTop = el.scrollHeight
    }
  })
})

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
  <!--
    Not a <dialog>, which is the one place this differs from every other panel in the app.
    showModal() makes the page behind it inert, and the whole point of this being a widget is
    that the memo list stays usable with an answer open beside it -- the answer is *about*
    those memos. So it is a plain fixed container, and the two things a modal would have given
    for free are done by hand: Escape closes it, and focus is moved on open and returned on
    close.

    The memo card a citation opens *is* a <dialog>, and that is the right way round: opening
    one is a task with its own controls, and it renders in the top layer above this.
  -->
  <div class="askw">
    <!--
      v-show rather than v-if, so the input keeps its text and the scroll position survives
      being closed and reopened. Closed it is `display: none`, which takes it out of the
      accessibility tree as well as off the screen -- so a screen reader does not walk an
      answer nobody has opened.

      No <Transition> around it, deliberately. The panel is animated on open by a keyframe
      animation in styles.css instead, and the note there has the measurement: a transition
      can be left unfinished by a browser that thinks the document is hidden, and a
      <Transition> that never finishes leaving is a panel that never gets `display: none`.
    -->
    <section
      v-show="open"
      id="ask-panel"
      class="askw__panel"
      role="dialog"
      aria-labelledby="ask-heading"
      @keydown.esc="hide"
    >
      <header class="askw__head">
        <h2 id="ask-heading" class="askw__heading">Ask your memos</h2>

        <button type="button" class="askw__close" @click="hide">
          <span aria-hidden="true">&times;</span>
          <span class="sr-only">Close</span>
        </button>
      </header>

      <p class="askw__hint">
        A question in your own words. A local model reads the few memos that match and
        answers from them — nothing leaves this machine.
      </p>

      <!--
        A <form>, so Enter submits without a keydown handler -- the same reason MemoComposer
        and ListFilters use one. submit.prevent because the default is a page reload.
      -->
      <form class="askw__form" @submit.prevent="ask">
        <input
          ref="inputEl"
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
          flips between Ask and Stop is one somebody presses expecting the other, and the two
          do opposite things.
        -->
        <button v-if="!busy" type="submit" :disabled="question.trim() === ''">Ask</button>

        <button v-else type="button" class="filters__clear" @click="stop">Stop</button>
      </form>

      <!--
        `busy` is in the condition as well as `answered`, and it is not redundant: the sources
        take a Postgres query to arrive and the answer takes seconds, so without it the panel
        shows nothing at all between the press and the first event. On a loaded machine that
        gap was two seconds of a page that looked like the button had done nothing.
      -->
      <div
        v-if="busy || answered || error || stopped"
        ref="resultEl"
        class="askw__result"
        @scroll="trackScroll"
      >
        <!--
          The question, above the answer to it. It is not decoration and it is not the same
          as the text in the box: the box is editable and is regularly halfway into a
          follow-up by the time an answer finishes, at which point nothing else on screen
          says what was asked.
        -->
        <p class="askw__asked">{{ asked }}</p>

        <!--
          aria-live, because the answer arrives after the press rather than because of it, and
          a screen reader would otherwise announce nothing at all. `polite` and not
          `assertive`: it is an answer somebody asked for, not an alert.

          aria-busy says the text is still being written, which is what stops a reader being
          given a half sentence as though it were the whole answer.
        -->
        <p v-if="answer !== ''" class="askw__answer" aria-live="polite" :aria-busy="busy">
          {{ answer }}<span v-if="busy" class="ask__cursor" aria-hidden="true"></span>
        </p>

        <!--
          Shown while the model is still working, which is the wait this panel exists to fill.
          Dropped as soon as the first token lands.

          Two sentences rather than one, because the two waits are different lengths and the
          reader can tell them apart: the search is a Postgres query and is over in
          milliseconds, and what follows is a local model reading. Saying "Reading 0 memos…"
          for the first of them would be both wrong and the more alarming of the two.
        -->
        <p v-else-if="busy" class="ask__waiting" role="status">
          <template v-if="sources.length === 0">Looking for memos that match…</template>
          <template v-else>
            Reading {{ sources.length }} {{ sources.length === 1 ? 'memo' : 'memos' }}…
          </template>
        </p>

        <!--
          A note rather than an error, because stopping is what the reader asked for. It is
          still said out loud: the answer above ends wherever the model had got to, and
          nothing else on screen distinguishes that from a sentence the model chose to end.
        -->
        <p v-if="stopped" class="ask__waiting" role="status">
          <template v-if="answer === ''">Stopped before the answer started.</template>
          <template v-else>Stopped — the answer above is as far as it got.</template>
        </p>

        <p v-if="error" class="notice notice--error" role="alert">{{ error }}</p>

        <!--
          The evidence. Every entry is a memo the answer was actually built from, and the
          excerpt is the same text the model was shown -- so checking a citation is checking
          what was read, not a different rendering of the memo.
        -->
        <ul v-if="shown.length > 0" class="ask__sources">
          <li v-for="source in shown" :key="source.id" class="ask__source">
            <span class="ask__ref" aria-hidden="true">[{{ source.ref }}]</span>

            <div class="ask__source-body">
              <!--
                A button rather than a link, because it opens a dialog rather than navigating
                -- the same reason MemoStrip's cards are buttons. Every citation is one now:
                a memo the strip is not holding is fetched by id rather than left unopenable.
              -->
              <button
                type="button"
                class="ask__open"
                :disabled="opening !== null"
                @click="openSource(source)"
              >
                {{ label(source) }}
              </button>

              <p class="ask__excerpt">
                {{ source.excerpt }}<template v-if="source.truncated"> (excerpt)</template>
              </p>
            </div>
          </li>
        </ul>

        <p v-if="openError" class="notice notice--error" role="alert">{{ openError }}</p>
      </div>
    </section>

    <!--
      The orb. A button with no visible label, which is why it carries one for a screen reader
      and a `title` for a pointer -- an unlabelled circle in a corner is a thing people hover
      before they click.

      aria-expanded and aria-controls make it a disclosure rather than an unexplained toggle:
      the panel it names is in the DOM whether it is open or not, so the reference always
      resolves.
    -->
    <button
      ref="orbEl"
      type="button"
      class="askw__orb"
      :aria-expanded="open"
      aria-controls="ask-panel"
      title="Ask your memos"
      @click="toggle"
    >
      <AskOrb :active="busy" :open="open" />

      <span class="sr-only">Ask your memos</span>
    </button>
  </div>
</template>

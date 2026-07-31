<script setup>
import { computed } from 'vue'
import { useMemos } from '../composables/useMemos'

/*
 * The filter box.
 *
 * `type="search"` rather than `type="text"`, for the semantics and for the Search key it
 * gets on an iOS keyboard -- not for the platform's own clear affordance, which WebKit
 * draws and Firefox does not. That one is switched off in styles.css and the Clear button
 * below replaces it, because one control that appears everywhere beats a native one on
 * some browsers and a second one beside it on others.
 *
 * A <form> wrapping one input, for the reason MemoComposer gives about its own: it tells
 * the browser what the field is for, which is where "press Enter to submit" comes from
 * rather than a keydown handler. submit.prevent because Enter already has a meaning here
 * -- filter now, without waiting out the debounce -- and a page reload is not it.
 */

/*
 * `pending` is MEMO-18's -- "something on this page is still going to change" -- and it is
 * exactly the question this file needs answered: those are the rows the API pinned into a
 * filtered page regardless of match, so they are why the count cannot be called a match
 * count. Reusing it rather than testing the statuses here is not only deduplication. This
 * file first shipped with its own `['queued','processing']`, which is the positive list
 * MEMO-18 exists to argue against: it is one value short the moment a status is added, and
 * MEMO-16 adds a retry path. Behind `pending`, an unknown status is treated as unfinished,
 * which is the answer that stays right.
 */
const { query, displayedFilter, memos, pending, search, searchNow, clearSearch } = useMemos()

/**
 * Whether to describe the list as filtered.
 *
 * `displayedFilter` carries the whole of that judgement -- it is null both while the box is
 * empty and before the first filtered response has arrived -- and it is shared with
 * MemoList so the two cannot disagree about when a filter is in effect. See the composable
 * for why the box and the echo each decide half of it.
 *
 * The count is the only condition added here, and only because MemoList's empty state
 * covers that case better; see the template.
 *
 * Gating on `loading` instead -- which this did at first -- gets the emptied-box case right
 * by accident and charges a flicker for it, hiding the line on every keystroke that reaches
 * the network. Nothing here is stale in a way that misleads: the count and the quoted query
 * always come from the same response, so there is no reason to blank it while the next one
 * is in the air.
 */
const isFiltered = computed(() => displayedFilter.value !== null && memos.value.length > 0)
</script>

<template>
  <form class="search" role="search" @submit.prevent="searchNow">
    <!--
      :value with @input rather than v-model. v-model would write the ref directly and
      the debounce would have nothing to hang off; search() sets the same ref and schedules
      the request, so the box stays responsive to every keystroke while the network does
      not.

      The thing that costs is v-model's composition guard: it suppresses its own writes
      between compositionstart and compositionend, because writing to an input mid-IME can
      cancel the composition, and a hand-rolled binding gets none of that. Checked rather
      than assumed, by counting writes through a patched value setter: typing seven
      characters produced *zero* writes to the element. Vue skips the DOM patch when the
      bound value already equals el.value, and search() sets the ref to exactly what the
      input just reported, so there is nothing to suppress. The one write in the whole
      interaction is clearSearch emptying the box, which is the case where interrupting a
      composition is the intent.
    -->
    <input
      :value="query"
      class="search__input"
      type="search"
      name="q"
      placeholder="Filter memos…"
      autocomplete="off"
      aria-label="Filter memos by text"
      @input="search($event.target.value)"
    />

    <button v-if="query !== ''" type="button" class="search__clear" @click="clearSearch">
      Clear
    </button>
  </form>

  <!--
    Only rendered while the list is filtered (see isFiltered), so an unfiltered list carries
    no extra furniture. The sentence itself quotes displayedFilter rather than the box, so it
    describes the rows underneath it: the count and the quoted query always come from the
    same response, instead of the number lagging a query the user has already replaced.

    Two wordings, because only one of them can honestly use the word "match". The API
    returns matches *and* memos pinned in because they are still being transcribed, and
    which row is which is not in the response -- a queued memo can match on its own
    transcript as easily as it can be pinned despite not matching. So the count is only
    called a match count when nothing is pinned, and otherwise says what it can defend:
    this is what is on screen for that filter. Getting this wrong is worse than it sounds
    -- "2 memos matching xylophone" over two rows that plainly do not contain the word
    reads as a broken search, which is the exact impression the line exists to prevent.

    Neither branch calls the number a total. It is what came back under the API's limit.

    The empty case is isFiltered's count condition rather than a third wording here, because
    MemoList's empty state already covers it and covers it better -- it suggests what to do
    about it. Both at once renders "0 memos match x" directly above "No memos match x",
    which reads like the page said it twice because something went wrong.
  -->
  <p v-if="isFiltered" class="search__status">
    <template v-if="pending">
      {{ memos.length === 1 ? '1 memo' : `${memos.length} memos` }} shown for
      <strong>{{ displayedFilter }}</strong
      >, including any still being processed
    </template>

    <template v-else>
      {{ memos.length === 1 ? '1 memo matches' : `${memos.length} memos match` }}
      <strong>{{ displayedFilter }}</strong>
    </template>
  </p>
</template>

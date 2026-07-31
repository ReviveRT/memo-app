<script setup>
import { useMemos } from '../composables/useMemos'

/*
 * The filter box.
 *
 * `type="search"` rather than `type="text"`, which is what gets the platform's own clear
 * affordance and, on iOS, a keyboard with a Search key. The explicit button below is kept
 * anyway: WebKit renders that native control only while the field is non-empty and Firefox
 * renders none at all, so it is an addition to the button rather than a replacement for
 * it.
 *
 * A <form> wrapping one input, for the reason MemoComposer gives about its own: it tells
 * the browser what the field is for, which is where "press Enter to submit" comes from
 * rather than a keydown handler. submit.prevent because Enter already has a meaning here
 * -- filter now, without waiting out the debounce -- and a page reload is not it.
 */
const { query, appliedQuery, memos, loading, search, searchNow, clearSearch } = useMemos()

/**
 * Mirrors Memo::IN_FLIGHT_STATUSES in the API, the same way MemoComposer mirrors
 * MAX_TEXT_LENGTH: two runtimes cannot share a constant, so the value is repeated with a
 * note saying where the other copy is. These are the statuses the search pins into a
 * filtered page regardless of match, which is the only reason this file needs to know
 * them.
 */
const IN_FLIGHT_STATUSES = ['queued', 'processing']

const hasInFlight = () => memos.value.some((memo) => IN_FLIGHT_STATUSES.includes(memo.status))
</script>

<template>
  <form class="search" role="search" @submit.prevent="searchNow">
    <!--
      :value with @input rather than v-model. v-model would write the ref directly and
      the debounce would have nothing to hang off; search() sets the same ref and schedules
      the request, so the box stays responsive to every keystroke while the network does
      not.
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
    Only rendered once a filter has actually been applied, so an unfiltered list carries no
    extra furniture. It reads appliedQuery rather than the box, so the sentence describes
    the rows underneath it: mid-debounce the count and the quoted query still agree with
    each other, instead of the number lagging a query the user has already replaced.

    Two wordings, because only one of them can honestly use the word "match". The API
    returns matches *and* memos pinned in because they are still being transcribed, and
    which row is which is not in the response -- a queued memo can match on its own
    transcript as easily as it can be pinned despite not matching. So the count is only
    called a match count when nothing is pinned, and otherwise says what it can defend:
    this is what is on screen for that filter. Getting this wrong is worse than it sounds
    -- "2 memos matching xylophone" over two rows that plainly do not contain the word
    reads as a broken search, which is the exact impression the line exists to prevent.

    Neither branch calls the number a total. It is what came back under the API's limit.

    Suppressed when nothing came back, because MemoList's empty state already says so and
    says it better -- it suggests what to do about it. Both at once renders "0 memos match
    x" directly above "No memos match x", which reads like the page said it twice because
    something went wrong.
  -->
  <p v-if="appliedQuery !== null && !loading && memos.length > 0" class="search__status">
    <template v-if="hasInFlight()">
      {{ memos.length === 1 ? '1 memo' : `${memos.length} memos` }} shown for
      <strong>{{ appliedQuery }}</strong
      >, including any still being processed
    </template>

    <template v-else>
      {{ memos.length === 1 ? '1 memo matches' : `${memos.length} memos match` }}
      <strong>{{ appliedQuery }}</strong>
    </template>
  </p>
</template>

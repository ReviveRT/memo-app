<script setup>
import { onMounted } from 'vue'
import MemoComposer from './components/MemoComposer.vue'
import MemoList from './components/MemoList.vue'
import MemoSearch from './components/MemoSearch.vue'
import { useMemos } from './composables/useMemos'
import { usePolling } from './composables/usePolling'

const { memos, pending, loading, busy, loadError, displayedFilter, load } = useMemos()

/*
 * The one place the list and the timer are joined, and the only file that knows both
 * exist. `pending` is the stop condition and lives with the statuses it reads;
 * everything about when to fire lives in usePolling.
 */
const { hinting } = usePolling(pending, () => load({ background: true }))

onMounted(() => load())
</script>

<template>
  <main class="app">
    <header class="app__header">
      <h1>Memos</h1>

      <!--
        Kept, now that the poll exists, because the poll deliberately stops: once every
        memo is `ready` or `failed` nothing on this screen is waiting for anything, and
        a timer left running against a finished list is a request every 5 seconds
        answering the same thing forever. A memo written from somewhere else -- a second
        tab, curl -- is then invisible until something asks, and this is what asks. It
        is also the way back when the tab was hidden long enough to be uninteresting.
      -->
      <button type="button" :disabled="loading" @click="load()">
        {{ loading ? 'Refreshing…' : 'Refresh' }}
      </button>
    </header>

    <MemoComposer />

    <!--
      Below the composer, not above it. Writing a memo is what this page is for and the
      filter is how you find one again, so the order matches: the thing you always do,
      then the thing you sometimes do. It also keeps the search box next to the list it
      filters rather than separated from it by a textarea.
    -->
    <MemoSearch />

    <!--
      The list's own error, kept above the list rather than replacing it: a failed
      refresh leaves the rows that did load on screen, and blanking them would look
      like the memos were gone.
    -->
    <p v-if="loadError" class="notice notice--error" role="alert">{{ loadError }}</p>

    <!--
      Non-blocking: nothing is disabled behind it, and role="status" is polite rather
      than assertive, so it cannot interrupt a screen reader mid-sentence the way the
      error banner's role="alert" is entitled to. The region is inserted along with its
      text rather than sitting in the DOM empty, which is the less dependable of the two
      arrangements for being announced at all -- it is what the banner above already
      does, and buying it back would mean a permanently empty box above the list.

      The wording aims at MEMO-10's voice path, which is the case that legitimately takes
      this long. A text memo reaches 45 seconds only when nothing picked it up -- both
      ai-worker replicas stopped, or one that died mid-job -- and usePolling.js is where
      the reason it keeps polling rather than giving up is written down.
    -->
    <p v-if="hinting" class="notice" role="status">
      Still transcribing — a long recording can take a while.
    </p>

    <!--
      `busy`, not `loading`. This component's job is to say why the list is empty, and a
      filter change that is still inside its debounce has not started a request yet -- so
      on `loading` it would answer that question from the previous filter's result. The
      button above stays on `loading`, which MEMO-18 narrowed to "a load somebody asked
      for", and a poll tick still leaves both alone.
    -->
    <MemoList
      :memos="memos"
      :loading="busy"
      :failed="Boolean(loadError)"
      :query="displayedFilter"
    />
  </main>
</template>

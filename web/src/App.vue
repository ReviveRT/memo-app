<script setup>
import { onMounted } from 'vue'
import MemoComposer from './components/MemoComposer.vue'
import MemoList from './components/MemoList.vue'
import { useMemos } from './composables/useMemos'
import { usePolling } from './composables/usePolling'

const { memos, pending, loading, loadError, load } = useMemos()

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
      The list's own error, kept above the list rather than replacing it: a failed
      refresh leaves the rows that did load on screen, and blanking them would look
      like the memos were gone.
    -->
    <p v-if="loadError" class="notice notice--error" role="alert">{{ loadError }}</p>

    <!--
      Non-blocking in both senses: nothing is disabled behind it, and role="status" is
      the polite live region, so a screen reader finishes the sentence it was on rather
      than interrupting to announce that waiting is still happening.

      The wording aims at MEMO-10's voice path, which is the case that legitimately
      takes this long. Today the only memo that can reach 45 seconds is a text one whose
      worker died mid-job, and there is nothing honest to say about that until MEMO-16's
      reaper exists to end it -- see usePolling.js.
    -->
    <p v-if="hinting" class="notice" role="status">
      Still transcribing — a long recording can take a while.
    </p>

    <MemoList :memos="memos" :loading="loading" :failed="Boolean(loadError)" />
  </main>
</template>

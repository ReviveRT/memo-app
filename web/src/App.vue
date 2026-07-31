<script setup>
import { onMounted } from 'vue'
import MemoComposer from './components/MemoComposer.vue'
import MemoList from './components/MemoList.vue'
import { useMemos } from './composables/useMemos'

const { memos, loading, loadError, load } = useMemos()

onMounted(load)
</script>

<template>
  <main class="app">
    <header class="app__header">
      <h1>Memos</h1>

      <!--
        A manual refresh, which is the honest control for the stack as it stands: the
        worker does not exist yet (MEMO-08), so a memo stays `queued` and nothing about
        it changes on its own. MEMO-18 replaces this with a poll that stops on a
        terminal status; until then a button beats a timer that would only ever fetch
        the same rows back.
      -->
      <button type="button" :disabled="loading" @click="load">
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

    <MemoList :memos="memos" :loading="loading" :failed="Boolean(loadError)" />
  </main>
</template>

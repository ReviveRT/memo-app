<script setup>
import { onMounted } from 'vue'
import MemoComposer from './components/MemoComposer.vue'
import MemoList from './components/MemoList.vue'
import MemoSearch from './components/MemoSearch.vue'
import { useMemos } from './composables/useMemos'

const { memos, loading, loadError, appliedQuery, load } = useMemos()

onMounted(load)
</script>

<template>
  <main class="app">
    <header class="app__header">
      <h1>Memos</h1>

      <!--
        A manual refresh, which is still the honest control for the stack as it stands,
        though no longer for the reason this comment used to give. It said the worker
        does not exist yet, so a memo stays `queued` and nothing about it changes on its
        own. MEMO-08 landed and both halves of that stopped being true: a replica claims
        the memo and it reaches `ready` about a second later -- and that second is the
        poll interval, not the work, which the MEMO-09 gate measured at 2-6ms. What is
        missing is not the transition but any way for the browser to hear about it, so
        the row on screen stays a snapshot of the moment it was submitted. MEMO-18
        replaces this with a poll that stops on a terminal status; until then the button
        is the only way to see a change that has already happened.
      -->
      <button type="button" :disabled="loading" @click="load">
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

    <MemoList
      :memos="memos"
      :loading="loading"
      :failed="Boolean(loadError)"
      :query="appliedQuery"
    />
  </main>
</template>

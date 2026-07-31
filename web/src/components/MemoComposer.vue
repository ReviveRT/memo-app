<script setup>
import { computed, ref } from 'vue'
import { useMemos } from '../composables/useMemos'

/**
 * Mirrors StoreMemoRequest::MAX_TEXT_LENGTH in the API, the same way config/memo.php
 * mirrors its own defaults: two runtimes cannot share a constant, so the number is
 * repeated with a note saying where the other copy is. The server stays authoritative
 * -- if these ever disagree, the 422 it answers with lands in the same slot as the
 * message below.
 */
const MAX_TEXT_LENGTH = 10_000

const { saving, saveError, submit } = useMemos()

const text = ref('')

const charCount = computed(() => text.value.trim().length)
const tooLong = computed(() => charCount.value > MAX_TEXT_LENGTH)
const canSubmit = computed(() => charCount.value > 0 && !tooLong.value && !saving.value)

async function onSubmit() {
  // Guarded here as well as by the disabled attribute, because Ctrl+Enter reaches this
  // without going through the button.
  if (!canSubmit.value) {
    return
  }

  if (await submit(text.value)) {
    text.value = ''
  }
}
</script>

<template>
  <!--
    No `maxlength` on the textarea, deliberately. It would silently truncate a paste
    over the cap -- the same quiet edit to somebody's memo that the API refuses to make
    when it rejects a NUL byte rather than stripping it. The count below says no
    instead, and the text stays intact to be shortened.

    submit.prevent because this is a real <form>: it gives the browser its own idea of
    what the screen is for, and the keyboard shortcut and the button then share one
    submit path.
  -->
  <form class="composer" @submit.prevent="onSubmit">
    <textarea
      v-model="text"
      class="composer__text"
      rows="4"
      placeholder="Type a memo…"
      autofocus
      @keydown.enter.meta.exact="onSubmit"
      @keydown.enter.ctrl.exact="onSubmit"
    />

    <div class="composer__actions">
      <!-- Only shown once it matters. A live "0 / 10,000" on an empty box is noise. -->
      <p v-if="tooLong" class="notice notice--error">
        {{ charCount.toLocaleString() }} / {{ MAX_TEXT_LENGTH.toLocaleString() }} characters — too
        long to store.
      </p>

      <span v-else class="composer__hint">⌘/Ctrl + Enter</span>

      <button type="submit" :disabled="!canSubmit">
        {{ saving ? 'Saving…' : 'Save memo' }}
      </button>
    </div>

    <p v-if="saveError" class="notice notice--error" role="alert">{{ saveError }}</p>
  </form>
</template>

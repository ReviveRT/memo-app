<script setup>
import { computed, ref } from 'vue'
import { useMemos } from '../composables/useMemos'
import { useRecorder } from '../composables/useRecorder'

/*
 * Record, stop, and how long it has been (MEMO-10).
 *
 * Above the textarea rather than below it, because this is what the app is for -- the
 * README's first line is "Record a voice memo or type one" -- and because the first
 * time somebody opens this page, the microphone is the thing that has to be findable
 * without looking.
 *
 * Everything about the microphone itself lives in useRecorder, and everything about
 * what happens to a memo afterwards lives in useMemos. What is left here is the two
 * buttons and the decision about which of several things to say in one line of status.
 */

const { saving, audioError, submitAudio } = useMemos()

const {
  recording,
  elapsedMs,
  error: microphoneError,
  start,
  stop,
  discard,
} = useRecorder()

/**
 * Whether the POST in flight is *this* component's.
 *
 * `saving` is shared with the composer -- one write at a time across the whole app --
 * so it cannot be used to say "uploading": a typed memo being saved would light this
 * component up too. It is still the right thing to disable the button on, which is why
 * both exist.
 */
const uploading = ref(false)

const elapsed = computed(() => formatElapsed(elapsedMs.value))

/**
 * Whichever went wrong: the microphone would not open, or the upload failed.
 *
 * One slot, because from where the user is standing there is one thing that did not
 * work, and two stacked banners about the same button read as two faults. They cannot
 * both be current -- start() clears the microphone error, and a recording that never
 * started cannot have failed to upload -- so this is a choice between one message and
 * nothing rather than a case of discarding half the story.
 *
 * Suppressed while recording, which is what keeps the last attempt's message from
 * sitting under a running timer describing a memo that has been replaced.
 */
const problem = computed(() => (recording.value ? null : (microphoneError.value ?? audioError.value)))

/**
 * Stop, and send what came back.
 *
 * A null result is not an error to report here: useRecorder resolves null when it has
 * already put the reason in `microphoneError`, or when the recording came back empty --
 * and it is the only thing that knows which.
 */
async function onStop() {
  const recorded = await stop()

  if (recorded === null) {
    return
  }

  uploading.value = true

  try {
    await submitAudio(recorded.blob, recorded.filename)
  } finally {
    uploading.value = false
  }
}

/**
 * Milliseconds as `m:ss`.
 *
 * Floored rather than rounded, so the display never reads 0:03 for a recording that has
 * not reached three seconds -- this number exists so somebody can judge the length
 * before sending, and it should not round up into a claim.
 *
 * No hours field. MAX_AUDIO_SECONDS is 600, so the longest memo this app intends to
 * accept shows as 10:00, and a recording left running past an hour reads 61:04 rather
 * than silently wrapping to 1:04.
 */
function formatElapsed(ms) {
  const total = Math.floor(ms / 1000)

  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}
</script>

<template>
  <!--
    No <form>, unlike the composer and the search box. There is no field here and
    nothing to submit: pressing Enter on a focused button already activates it, which is
    the whole of what a form would have added.
  -->
  <section class="recorder" aria-label="Record a voice memo">
    <button v-if="!recording" type="button" :disabled="saving" @click="start()">
      {{ uploading ? 'Uploading…' : 'Record' }}
    </button>

    <template v-else>
      <button type="button" @click="onStop">Stop</button>

      <!--
        Throwing the recording away is a separate button rather than a confirmation on
        Stop, because Stop is the common case and should cost one click. This one keeps
        a mis-started recording from having to be uploaded and then lived with -- there
        is no delete yet (nothing owns it before MEMO-17), so before the POST is the
        only moment a recording can be reconsidered.
      -->
      <button type="button" class="recorder__discard" @click="discard">Discard</button>

      <!--
        role="timer" rather than a live region: it is the ARIA role for exactly this, and
        its implicit aria-live is "off", so a screen reader offers the value on demand
        instead of reading a new number aloud five times a second.
      -->
      <span class="recorder__elapsed" role="timer" aria-label="Recording length">
        <span class="recorder__dot" aria-hidden="true"></span>
        {{ elapsed }}
      </span>
    </template>

    <span v-if="!recording && !uploading" class="recorder__hint">
      Speak a memo — it is transcribed after you stop.
    </span>
  </section>

  <p v-if="problem" class="notice notice--error" role="alert">{{ problem }}</p>
</template>

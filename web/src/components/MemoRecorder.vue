<script setup>
import { computed, ref } from 'vue'
import ProgressBar from './ProgressBar.vue'
import { CLOUD_ANCHOR_ID } from '../cloudAnchor'
import { AUTO_DETECT, LANGUAGES } from '../languages'
import { useMemos } from '../composables/useMemos'
import { useRecorder } from '../composables/useRecorder'

/*
 * Record, submit, and how long it has been (MEMO-10).
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

const { uploading, uploadProgress, audioError, submitAudio } = useMemos()

const {
  recording,
  elapsedMs,
  error: microphoneError,
  start,
  stop,
  discard,
} = useRecorder()

const elapsed = computed(() => formatElapsed(elapsedMs.value))

/**
 * What to decode the next recording in. Empty means detect it, which is the default.
 *
 * Component state rather than a store, and it deliberately survives a submission: somebody who
 * has just told the app they are speaking Romanian is probably about to record another Romanian
 * memo, and resetting to Auto-detect after every one would make the control something you have
 * to set every single time. It resets on reload, which is the right amount of stickiness for a
 * setting with no account behind it.
 *
 * Not persisted to localStorage for the same reason it is not global: a language silently
 * remembered from last week, applied to a memo in a different one, is the failure this whole
 * control exists to fix -- just with the app doing the misdetecting instead of the model.
 */
const language = ref(AUTO_DETECT)

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
 * Stop the microphone, and send what came back.
 *
 * The button that calls this says **Submit**, not Stop, and the two words describe the
 * same click from different ends of it. "Stop" names what happens to the microphone;
 * "Submit" names what happens to the memo -- and the memo is the thing being decided
 * about. Beside a Discard button, "Stop" also reads as the neutral of the pair, as
 * though the recording then sits somewhere waiting to be sent, when in fact this posts
 * it. There is no third state, and the label now says which of the two buttons keeps
 * the memo.
 *
 * A null result is not an error to report here: useRecorder resolves null when it has
 * already put the reason in `microphoneError`, or when the recording came back empty --
 * and it is the only thing that knows which.
 */
async function onSubmit() {
  const recorded = await stop()

  if (recorded === null) {
    return
  }

  // `uploading` is held by submitAudio itself rather than around it here, so the flag
  // the button reads and the flag that guards re-entry are the same one. Set here, the
  // two could disagree -- and it is the guard, not the label, that decides whether this
  // recording is posted at all.
  // `|| null`, so the Auto-detect option's empty string reaches the API as an absent field
  // rather than as an empty one. Both mean "detect it" server-side, but the request is more
  // legible without it -- see createVoiceMemo.
  await submitAudio(recorded.blob, recorded.filename, language.value || null)
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
    <!--
      Disabled on `uploading` and not on the composer's `saving`: a typed memo being
      saved is no reason this button cannot be pressed, and the two write paths are
      independent all the way to two rows.
    -->
    <!--
      The id is MemoBackdrop's anchor: the bloom behind the page is centred on
      whichever of these two buttons is on screen, so the light reads as coming
      off the control rather than sitting at an arbitrary point. It is on both
      because they are mutually exclusive -- exactly one exists at any moment, so
      the id stays unique -- and the backdrop re-measures when this row changes
      shape. Moving it to the section would put the bloom under the hint text
      instead, which is the one place here that has to stay readable.

      Bound from cloudAnchor.js rather than written out, because the landing page
      claims the same id when this screen is not mounted. One string, three
      consumers; that module has the rule that only one element may carry it.
    -->
    <button
      v-if="!recording"
      :id="CLOUD_ANCHOR_ID"
      type="button"
      :disabled="uploading"
      @click="start()"
    >
      {{ uploading ? 'Uploading…' : 'Record' }}
    </button>

    <template v-else>
      <button :id="CLOUD_ANCHOR_ID" type="button" @click="onSubmit">Submit</button>

      <!--
        Throwing the recording away is a separate button rather than a confirmation on
        Submit, because submitting is the common case and should cost one click. This one
        keeps a mis-started recording from having to be uploaded and then dealt with
        afterwards -- and it is still worth having now that a memo can be deleted, because
        discarding never creates a row, never spends a worker on it, and never puts audio
        on the volume.
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

    <!--
      The language for the next recording.

      **Visible before recording and not during it**, which is the opposite of what a first
      draft does. The choice has to be made before Submit to have any effect, so offering it
      mid-recording would invite somebody to change it while talking and expect the change to
      apply -- and it would, but only because the value is read at Submit, which is a coupling
      nobody should have to reason about. Hidden while uploading too: by then the memo is gone
      and the field would be describing the next one while looking like it described this one.

      Disabled rather than hidden during `uploading` would keep the layout stable, and is worse:
      a greyed-out control that reappears enabled a second later reads as something the app is
      doing, not as something waiting for you.

      A plain <select> rather than a search-as-you-type combobox. Thirty-odd options is inside
      what a native select handles well, and the native one is the only version that gets
      keyboard support, screen-reader announcement and the platform's own scrolling for free --
      on a phone it becomes the OS picker, which is better than anything here would be.
    -->
    <label v-if="!recording && !uploading" class="recorder__language">
      <span class="recorder__language-label">Language</span>

      <select v-model="language" class="recorder__language-select">
        <!--
          First, and the default. Detection is right most of the time and this control exists
          for when it is not -- so the honest ordering puts the automatic choice at the top
          rather than making everyone pick from a list on every memo.
        -->
        <option :value="AUTO_DETECT">Auto-detect</option>

        <option v-for="option in LANGUAGES" :key="option.code" :value="option.code">
          {{ option.name }}
        </option>
      </select>
    </label>

    <span v-if="!recording && !uploading" class="recorder__hint">
      Speak a memo — it is transcribed after you submit.
    </span>
  </section>

  <!--
    Only while the bytes are going out. Once they are gone the wait belongs to the memo,
    and the row's own bar picks it up -- two bars for one recording would suggest two
    things were happening.

    The bar carries the label rather than a caption beside it: the button already reads
    "Uploading…", and a screen reader hearing that plus "Sending recording, 40 percent"
    is being told once by each.
  -->
  <ProgressBar
    v-if="uploading"
    class="recorder__progress"
    label="Sending recording"
    :value="uploadProgress"
  />

  <p v-if="problem" class="notice notice--error" role="alert">{{ problem }}</p>
</template>

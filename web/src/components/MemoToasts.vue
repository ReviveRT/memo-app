<script setup>
import ProgressBar from './ProgressBar.vue'
import { useMemoToasts } from '../composables/useMemoToasts'
import { useProcessingProgress } from '../composables/useProcessingProgress'

/*
 * The corner cards that follow a memo from the button press to the transcript.
 *
 * All of the state is in useMemoToasts and all of the *numbers* are somewhere else again --
 * that file has the argument for why a toast stores which stage it is in and nothing about how
 * full its bar should be. What is left here is the wording for each stage and the choice of
 * which of the two bars to draw.
 *
 * The two bars are genuinely different measurements wearing the same component. The upload one
 * is a real fraction of a real byte count, reported by XMLHttpRequest; the transcription one is
 * an estimate from a curve that approaches 90% and never arrives, because nothing anywhere
 * knows how far along a whisper decode is. ProgressBar is built to tell those apart -- a null
 * value is announced as "busy" rather than as a percentage -- so the honesty is preserved
 * without this file having to say anything about it.
 */

const { toasts, dismiss, PHASES } = useMemoToasts()

/*
 * The same estimate the memo's own card draws, from the same singleton keyed by the same id.
 *
 * Deliberately shared rather than a second curve started here. Two bars for one memo that
 * disagree by a few percent is worse than either bar alone: it reads as one of them being
 * wrong, and there is no way for the user to tell which.
 */
const { progressFor } = useProcessingProgress()

/**
 * What the card says it is doing, one line, from the stage -- and, for the last one, from what
 * was being attempted.
 *
 * The default arm is REJECTED, and it is the one place the stage is not enough. "The write did
 * not land" covers a memo that was never stored and a retry the API refused, and those are not
 * the same sentence: nothing was being saved in the second case, so "Not saved" would describe
 * an action nobody took. See the `kind` field in useMemoToasts.
 */
function title(toast) {
  switch (toast.phase) {
    case PHASES.UPLOADING:
      return 'Sending recording…'
    case PHASES.SAVING:
      return 'Saving memo…'
    case PHASES.RETRYING:
      return 'Sending it back…'
    case PHASES.QUEUED:
      return 'Waiting for a worker…'
    case PHASES.PROCESSING:
      return 'Transcribing…'
    case PHASES.READY:
      return 'Memo ready'
    case PHASES.FAILED:
      return 'Could not transcribe'
    default:
      return toast.kind === 'retry' ? 'Could not retry' : 'Not saved'
  }
}

const isError = (toast) => toast.phase === PHASES.FAILED || toast.phase === PHASES.REJECTED

const isDone = (toast) => isError(toast) || toast.phase === PHASES.READY

/**
 * Which bar this stage draws, or null for none.
 *
 * Returned as a value rather than as two `v-if`s in the template, because the difference
 * between "a measured fraction" and "an estimate" is exactly the difference between passing a
 * number and passing null -- and having one function decide it keeps the two from drifting into
 * meaning something else.
 */
function fraction(toast) {
  // RETRYING alongside the two write stages, and it is the clearest case for null of the
  // three: the request is one bodyless POST, so there is not even a byte count to not know.
  if (
    toast.phase === PHASES.UPLOADING ||
    toast.phase === PHASES.SAVING ||
    toast.phase === PHASES.RETRYING
  ) {
    // null here is not "no progress": it is what a request whose total size the browser will
    // not disclose reports, and what a typed memo's POST never reports at all. ProgressBar
    // draws motion rather than a number for it. See api/memos.js.
    return toast.uploadFraction
  }

  // The estimate, keyed by the memo the toast is attached to. `memoId` is set the moment the
  // API answers, so it is never null in these two stages.
  return progressFor(toast.memoId)
}

const hasBar = (toast) => !isDone(toast)
</script>

<template>
  <!--
    role="status" with the implicit polite live region: a memo finishing is worth announcing,
    and it is never urgent enough to interrupt somebody mid-sentence. Same reasoning as the
    reminder banner beside it, which is why the two share a corner and a stylesheet.

    The region is in the DOM whether or not it holds anything, because a live region inserted
    together with its first message is the less dependable of the two arrangements -- and every
    message here is inserted by a request rather than by a user action on the region itself.
  -->
  <div class="toasts__group" role="status" aria-live="polite">
    <div
      v-for="toast in toasts"
      :key="toast.id"
      class="toast"
      :class="{ 'toast--error': isError(toast), 'toast--done': toast.phase === PHASES.READY }"
    >
      <div class="toast__body">
        <p class="toast__title">{{ title(toast) }}</p>

        <!--
          The memo's label once there is one, or the reason it failed. Both are the same slot
          because both answer "which memo, and what happened to it" -- see describe() in
          useMemoToasts.
        -->
        <p v-if="toast.detail" class="toast__note">{{ toast.detail }}</p>

        <ProgressBar
          v-if="hasBar(toast)"
          class="toast__progress"
          :label="title(toast)"
          :value="fraction(toast)"
        />
      </div>

      <!--
        Present at every stage, including the ones still running. Dismissing a toast mid-upload
        does not cancel anything -- the memo is still being written, and its card in the strip
        still says so -- it only takes the receipt off the screen. That is the right thing for a
        button in the corner of the page to do: nothing here should be able to destroy work.
      -->
      <button
        type="button"
        class="toast__close"
        aria-label="Dismiss"
        @click="dismiss(toast.id)"
      >
        ×
      </button>
    </div>
  </div>
</template>

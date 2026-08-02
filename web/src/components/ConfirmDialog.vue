<script setup>
import { nextTick, ref, watch } from 'vue'
import { useConfirm } from '../composables/useConfirm'

/*
 * The app's own "are you sure?".
 *
 * Mounted once, by MemosView, and driven entirely by useConfirm -- that module has the argument
 * for why this replaced `window.confirm` and why the API is a promise.
 *
 * A native <dialog> with showModal(), like the two sheets, and for the same four reasons: focus
 * is trapped, Escape closes it, everything behind is inert, and it renders in the top layer. The
 * last one is what makes this work *over* a sheet -- a memo's delete is asked from inside an
 * already-open dialog, and modal dialogs stack in the top layer in the order they were opened,
 * so this lands above it without a z-index. styles.css's "nothing needs a z-index" rule holds.
 */

const { question, answer } = useConfirm()

const dialogEl = ref(null)
const cancelEl = ref(null)

watch(
  question,
  async (asked) => {
    const el = dialogEl.value

    if (!el) {
      return
    }

    if (asked && !el.open) {
      el.showModal()

      // Focus lands on Cancel rather than on the confirm button, and that is the whole of this
      // component's opinion about safety. A destructive action should never be one Enter away
      // from a dialog that has just appeared -- somebody finishing a keystroke on the button
      // that opened this would confirm it without reading a word.
      await nextTick()
      cancelEl.value?.focus()
    } else if (!asked && el.open) {
      el.close()
    }
  },
  { flush: 'post' },
)

/*
 * Escape and the backdrop both mean no.
 *
 * `@close` fires for Escape and for close(), so answering there covers the keyboard without a
 * keydown handler -- and answer() is idempotent enough for the double call that a button press
 * produces, since it clears `settle` before resolving.
 */
</script>

<template>
  <dialog
    ref="dialogEl"
    class="confirm"
    @close="answer(false)"
    @click="$event.target === dialogEl && answer(false)"
  >
    <div v-if="question" class="confirm__body">
      <!--
        A heading rather than a paragraph, because it is the question and a screen reader
        should reach it as the dialog's name. `aria-labelledby` is not needed: a <dialog> with
        a heading as its first content is announced from it.
      -->
      <h2 class="confirm__title">{{ question.title }}</h2>

      <p v-if="question.body" class="confirm__note">{{ question.body }}</p>

      <!--
        Cancel first in the DOM, so it is the first thing Tab reaches and the first thing a
        screen reader hears -- the safe option should be the default in every ordering. It is
        placed to the *right* of Confirm visually by `row-reverse`, which is where a primary
        action sits on the platforms this is read on, so the two orders differ on purpose.
      -->
      <div class="confirm__actions">
        <button ref="cancelEl" type="button" class="ghost" @click="answer(false)">Cancel</button>

        <button
          type="button"
          :class="question.danger ? 'confirm__go confirm__go--danger' : 'confirm__go'"
          @click="answer(true)"
        >
          {{ question.confirmLabel }}
        </button>
      </div>
    </div>
  </dialog>
</template>

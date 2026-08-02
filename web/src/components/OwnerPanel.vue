<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { claimLink, claimOutcome } from '../composables/useOwner'

/*
 * "These memos live in this browser", and the one control that does something about it.
 *
 * **Why this screen needs to say anything at all.** There is no account here: the memos on
 * this page belong to a cookie, and nothing else in the UI would ever tell you that. Somebody
 * who records a week of memos on a laptop, opens the app on a phone and finds it empty has
 * been given no way to understand what happened -- and the honest explanation is short.
 * Saying it once, quietly, next to the fix, is cheaper than the support question.
 *
 * The link is revealed on demand rather than rendered on load. It is a bearer credential:
 * whoever has it has the memos, with no second factor and no way to revoke it. So it is not
 * something to leave sitting on screen behind whoever walks past, and the request that
 * produces it is a POST for the same reason -- see api/owner.js.
 */

const route = useRoute()
const router = useRouter()

/** The claim link once it has been asked for, or null. Cleared, never cached across mounts. */
const link = ref(null)

const loading = ref(false)
const error = ref(null)
const copied = ref(false)

/** Whether the explanation is expanded. Collapsed by default: it is reference, not news. */
const open = ref(false)

/*
 * The result of following a claim link, read off `?claim=`.
 *
 * Computed once at setup rather than as a `computed`, because it is consumed and then
 * removed: leaving the parameter in the URL means the notice comes back on every reload and
 * on the back button, long after it stopped being true.
 */
const outcome = ref(claimOutcome(route.query.claim))

if (outcome.value !== null) {
  // replace, not push, so the back button does not walk into a URL that re-announces a claim
  // that happened minutes ago. The rest of the query is preserved -- filters live there.
  const { claim, ...rest } = route.query

  router.replace({ query: rest })
}

async function reveal() {
  loading.value = true
  error.value = null

  try {
    link.value = await claimLink()
  } catch (cause) {
    // api/request.js has already turned this into a sentence written for a reader.
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function copy() {
  // navigator.clipboard is absent on http origins other than localhost, which is exactly how
  // this app is served in development on a LAN address. Falling back to selecting the text is
  // not worth the code -- the input is already selectable and visible -- but silently doing
  // nothing is, so the failure is reported rather than swallowed.
  try {
    await navigator.clipboard.writeText(link.value)
    copied.value = true

    setTimeout(() => {
      copied.value = false
    }, 2000)
  } catch {
    error.value = 'Could not copy automatically. Select the link and copy it.'
  }
}
</script>

<template>
  <section class="owner">
    <p v-if="outcome" class="notice" :class="`notice--${outcome.tone}`" role="status">
      {{ outcome.message }}
    </p>

    <button type="button" class="owner__toggle" :aria-expanded="open" @click="open = !open">
      {{ open ? 'Hide' : 'These memos are saved to this browser' }}
    </button>

    <div v-if="open" class="owner__body">
      <p class="owner__explainer">
        There is no account. Your memos belong to this browser, and clearing its cookies or
        switching to another device will show an empty list. To use the same memos somewhere
        else, open the link below there.
      </p>

      <button type="button" :disabled="loading" @click="reveal">
        {{ loading ? 'Getting the link…' : 'Show my link' }}
      </button>

      <div v-if="link" class="owner__link">
        <!--
          readonly rather than disabled: a disabled input cannot be selected, and selecting
          the text by hand is the fallback when the clipboard API is unavailable.
        -->
        <input :value="link" readonly class="owner__url" @focus="$event.target.select()" />

        <button type="button" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button>

        <p class="owner__warning">
          Anyone with this link can read and change these memos. Treat it like a password.
        </p>
      </div>

      <p v-if="error" class="notice notice--error" role="alert">{{ error }}</p>
    </div>
  </section>
</template>

/*
 * Who this browser is, resolved once per page load.
 *
 * Module scope rather than per-component state, and that is the whole design: the bootstrap
 * must happen exactly once no matter how many components want it, and every one of them must
 * wait for the same call rather than starting its own. See ensureOwner.
 */

import { ref } from 'vue'

import { fetchClaimLink, fetchOwner } from '../api/owner'

/** The resolved owner id, or null before the bootstrap has finished. Not a secret. */
export const ownerId = ref(null)

/**
 * The in-flight bootstrap, memoised.
 *
 * A promise and not a boolean, because the thing callers need is not "has it started" but
 * "tell me when it is done" -- and several of them ask at once. MemosView awaits this before
 * loading anything; a flag would have let the second caller through while the first request
 * was still open, which is the exact race the bootstrap exists to prevent.
 */
let pending = null

/**
 * Make sure this browser has an identity before anything else is fetched.
 *
 * Idempotent and safe to call from anywhere. The first call makes the request; every later
 * one gets the same promise back, including calls that arrive while it is still open.
 *
 * **A failure is not rethrown and not retried.** The API mints an owner for this route
 * whether or not the browser had one, so the only way it fails is a stack that is down --
 * in which case the requests this was gating are about to fail too, with error handling of
 * their own that is written for a reader (see api/request.js). Rejecting here would replace
 * those sentences with an unhandled rejection at boot, before anything is on screen.
 *
 * The promise is cleared on failure so a later caller can try again, which is what makes a
 * transient blip recoverable without a reload.
 */
export function ensureOwner() {
  if (pending === null) {
    pending = fetchOwner()
      .then((owner) => {
        ownerId.value = owner.id

        return owner
      })
      .catch(() => {
        pending = null

        return null
      })
  }

  return pending
}

/**
 * Fetch the shareable link. Deliberately not cached -- see api/owner.js.
 */
export function claimLink() {
  return fetchClaimLink()
}

/**
 * What `?claim=` in the URL means, as a sentence.
 *
 * The API redirects here after a claim link is opened, and the three outcomes need three
 * different things said. Kept next to the bootstrap rather than in the view because it is
 * part of the same protocol, and because the strings are the only place the *user* is told
 * what an owner is.
 *
 * Returns null for anything else, including no parameter at all, so a caller can render on
 * truthiness without a second check.
 */
export function claimOutcome(value) {
  if (value === 'ok') {
    return {
      tone: 'ok',
      message: 'These memos are now on this device too. The link keeps working — open it anywhere else you want them.',
    }
  }

  if (value === 'unknown') {
    return {
      tone: 'error',
      // Deliberately not "that link is wrong". The overwhelmingly likely cause is an owner
      // pruned after a year of not being used, or a database that has since been reset --
      // and telling somebody their correctly-copied link is invalid sends them looking for
      // a typo that is not there.
      message: 'That link no longer points at any memos. It may have expired after a long time unused.',
    }
  }

  if (value === 'invalid') {
    return {
      tone: 'error',
      message: 'That link is not complete. Copy the whole thing, including the part after the last slash.',
    }
  }

  return null
}

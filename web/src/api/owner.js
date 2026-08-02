/*
 * The two calls that decide, and reveal, whose memos this browser is looking at.
 *
 * There is no login here and no token in this file. The identity is an HttpOnly cookie the
 * API sets, which means script on this page cannot read it, cannot forge one, and does not
 * have to attach it -- the browser does that on every same-origin request by itself. That is
 * why memos.js and collections.js needed no changes at all when memos became per-owner.
 *
 * It is also the reason the identity is a cookie rather than a value in localStorage, which
 * is the obvious first design. Two things rule that out, and the second is the one that
 * cannot be worked around:
 *
 *   1. Anything script can read, injected script can read and keep.
 *   2. The recording in MemoDialog is an `<audio src="/api/memos/.../audio">`. The *browser*
 *      issues that request, not this code, so there is no fetch wrapper to attach a header
 *      to. A localStorage identity would have left every recording readable by memo id
 *      alone -- silently, because every JSON route would have looked correctly scoped.
 */

import { request } from './request'

/**
 * Establish the identity for this browser.
 *
 * **Awaited before anything else fetches**, which is what makes it a bootstrap rather than
 * one more request. This is the only safe read the API will mint an owner for, so calling it
 * first means every later request carries a cookie. Fire the list, the collections and the
 * reminders in parallel without it and each arrives with no cookie: the API answers them from
 * an empty transient owner, and this call then mints a *different* identity -- so a cold load
 * would show nothing and the memos written afterwards would land somewhere the next reload
 * could not find.
 *
 * @returns {Promise<{id: string}>} The owner id, which is not a secret and grants nothing.
 *   Useful only for telling "the same person as last time" from a fresh identity.
 */
export async function fetchOwner() {
  const body = await request('/api/owner')

  return body.owner
}

/**
 * The shareable link that moves these memos to another browser.
 *
 * A POST, and the API insists on that for a reason worth repeating here so nobody
 * "corrects" it: this is the one response in the whole application that contains the bearer
 * token. Behind a POST it is produced only when somebody presses the button, rather than
 * sitting in the memory of every open tab and in the logs of anything that records response
 * bodies for reads.
 *
 * Nothing caches the result. The link is stable, so caching it would work -- and it would
 * mean holding a credential in module scope for the lifetime of the page, which is exactly
 * what the POST was chosen to avoid.
 *
 * @returns {Promise<string>} An absolute URL. Opening it in any browser adopts these memos
 *   there. Anyone who has it has the memos -- there is no second factor and no revocation.
 */
export async function fetchClaimLink() {
  const body = await request('/api/owner/claim-link', { method: 'POST' })

  return body.claim_url
}

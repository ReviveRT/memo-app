/*
 * The four collection calls, and the only place in this app that knows their wire format.
 *
 * A file of its own rather than more of memos.js, because a collection is a different
 * resource with a different envelope -- `{"collections": [...]}` and `{"collection": {...}}`.
 * Reminders are the counter-example and are deliberately *not* here: every reminder route
 * answers with the memo it belongs to, so those calls return memos.js's shape and live
 * there.
 *
 * The query string is built by memos.js's `filterQueryString`, imported rather than
 * reimplemented. That is the one thing about this file worth being deliberate about: the
 * brief asks for the same search and the same date filter over collections as over memos,
 * and the API honours that by taking identically spelled parameters (ListCollectionsRequest
 * says so from its side). Sharing the builder is what stops the two screens drifting into
 * sending `?search=` on one and `?q=` on the other.
 */
import { filterQueryString } from './memos'
import { request } from './request'

/**
 * GET /api/collections -- newest first, filtered by name-or-contents and by creation date.
 *
 * The `q` here reaches further than it looks: the API matches the collection's name **or**
 * any memo filed inside it, so searching "dentist" finds the collection holding the dentist
 * memo even though its name is "Errands". See CollectionRepository::list for why that is the
 * useful reading of "the same search".
 *
 * `collection` is not among the filters -- there is nothing for a collection to be scoped to
 * -- and passing it would simply be ignored by the API's validation.
 *
 * @param {{query?: ?string, from?: ?string, to?: ?string}} [filter]
 * @returns {Promise<{collections: Array<object>, query: ?string, from: ?string, to: ?string}>}
 */
export async function listCollections(filter = {}) {
  const body = await request(`/api/collections${filterQueryString(filter)}`)

  return {
    // Defensive for the reason listMemos is: `v-for` over a non-iterable throws inside the
    // render function, and the stack trace names the grid rather than the response.
    collections: Array.isArray(body?.collections) ? body.collections : [],

    query: typeof body?.query === 'string' ? body.query : null,
    from: typeof body?.from === 'string' ? body.from : null,
    to: typeof body?.to === 'string' ? body.to : null,
  }
}

/**
 * POST /api/collections -- create one with the name the user typed.
 *
 * A duplicate name is a 422 whose `message` reads "You already have a collection called
 * ...", which reaches the screen through request()'s error branch like any other validation
 * failure. Nothing special is done for it here: it is the user's ordinary mistake and the
 * API has already worded it.
 *
 * @param {string} name
 * @returns {Promise<object>} The stored collection, with `memo_count` 0 and no labels.
 */
export async function createCollection(name) {
  return stored(
    await request('/api/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  )
}

/**
 * PATCH /api/collections/{id} -- rename one.
 *
 * Renaming to the collection's current name is a successful no-op rather than a duplicate,
 * because the unique index compares the row against itself. That matters for the UI: the
 * card's rename field starts out holding the existing name, so submitting it unchanged is
 * the easiest thing a user can accidentally do.
 *
 * @param {string} id
 * @param {string} name
 * @returns {Promise<object>} The collection, renamed, with its count and labels intact.
 */
export async function renameCollection(id, name) {
  return stored(
    await request(`/api/collections/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  )
}

/**
 * DELETE /api/collections/{id} -- remove the collection, keep its memos.
 *
 * The memos are not deleted: `ON DELETE SET NULL` on `memos.collection_id` returns them to
 * the fast strip. The caller therefore has to reload the memo list as well as the grid,
 * because the strip has just grown by however many memos this was holding -- and nothing in
 * this response says how many, since the API answers 204.
 *
 * Returns nothing. request() turns the 204 into null rather than mistaking a bodiless
 * success for a proxy failure, which it would otherwise do -- a 204 carries no Content-Type.
 *
 * @param {string} id
 * @returns {Promise<void>}
 */
export async function deleteCollection(id) {
  await request(`/api/collections/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** The row out of an envelope, or a readable error if the envelope was empty. */
function stored(body) {
  if (!body?.collection) {
    throw new Error('The API accepted the collection but did not return it.')
  }

  return body.collection
}

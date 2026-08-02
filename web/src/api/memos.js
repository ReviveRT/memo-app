/*
 * Every call this app makes about a memo, and the only place in it that knows the
 * wire format. Collections have their own file; reminders live here, because every
 * reminder route answers with the memo it belongs to and so returns this file's shape.
 *
 * Relative paths on purpose -- `/api/memos`, not an absolute base URL. The dev
 * server proxies /api to the API container (vite.config.js), so same-origin is what
 * makes this app need no CORS handling, no base-URL variable and no build-time
 * configuration at all.
 *
 * Every response is an envelope, `{"memos": [...]}` and `{"memo": {...}}`, rather
 * than a bare array and a bare row. That was decided for a search that did not exist
 * yet, and the room has been used twice over: the list carries `query` and now also
 * `from`, `to` and `collection`. Nothing already read here has changed type -- `query`
 * is still a string or null -- see api/app/Http/Controllers/MemoController.php.
 */
import { JSON_CONTENT_TYPE, errorMessage, request } from './request'

/**
 * Builds a query string from a filter, leaving out everything that is not set.
 *
 * URLSearchParams rather than a template string, and it is load-bearing rather than
 * tidy: the query is somebody's raw typing, and `&`, `#`, `+` and `%` all mean something
 * in a query string. Interpolated raw, `milk & eggs` arrives as `q=milk ` plus a stray
 * parameter named `eggs`, `+` decodes to a space, and a bare `%` is the start of an escape
 * sequence rather than a percent sign. Encoded, each of them reaches the API as the
 * character that was typed. The API then escapes the same string again on its own side,
 * for LIKE rather than for URLs -- two different syntaxes, two separate escapes, neither
 * standing in for the other.
 *
 * Absent parameters are omitted rather than sent empty. `?q=` and no `q` at all mean the
 * same thing to the API -- both collapse to "no filter" -- but omitting them keeps the
 * common request to one canonical form, which is what makes a URL in a network log worth
 * reading.
 *
 * Exported because collections.js sends the same three of these against its own route: the
 * search box and the date filter behave identically on both screens, which is a property of
 * the API's contract (see ListCollectionsRequest) and is kept true here by the two callers
 * building their query strings with one function.
 *
 * @param {{query?: ?string, from?: ?string, to?: ?string, collection?: ?string}} filter
 * @returns {string} Including the leading `?`, or empty when nothing is filtered.
 */
export function filterQueryString({ query = null, from = null, to = null, collection = null }) {
  const params = new URLSearchParams()

  if (query !== null && query !== '') {
    params.set('q', query)
  }

  // The two dates are ISO instants, not calendar dates: the browser turns "yesterday" into a
  // pair of absolute times because only it knows the reader's timezone. See useDateRange,
  // and App\Support\TimeWindow on the other side, for why the interval is half-open.
  if (from !== null) {
    params.set('from', from)
  }

  if (to !== null) {
    params.set('to', to)
  }

  // 'none' for the fast strip, an id for one collection, absent for everything. One
  // parameter with three readings, so a request cannot ask for both at once.
  if (collection !== null) {
    params.set('collection', collection)
  }

  const encoded = params.toString()

  return encoded === '' ? '' : `?${encoded}`
}

/**
 * GET /api/memos -- newest first, capped by the API's own default limit of 50, and narrowed
 * by whichever of the four filters are set.
 *
 * @param {{query?: ?string, from?: ?string, to?: ?string, collection?: ?string}} [filter]
 * @returns {Promise<{memos: Array<object>, query: ?string, from: ?string, to: ?string,
 *   collection: ?string}>} The rows, and the filters the API says they came back for. See
 *   useMemos for what the echo is used for.
 */
export async function listMemos(filter = {}) {
  const body = await request(`/api/memos${filterQueryString(filter)}`)

  return {
    // Defensive because the alternative is a template crash: `v-for` over a
    // non-iterable throws inside the render function, and the stack trace names
    // MemoStrip rather than the response that caused it.
    memos: Array.isArray(body?.memos) ? body.memos : [],

    // Each normalised to null for anything that is not a string, so callers compare against
    // one absent value rather than against null, undefined and a missing key.
    query: echoed(body?.query),
    from: echoed(body?.from),
    to: echoed(body?.to),
    collection: echoed(body?.collection),
  }
}

/** One echoed filter, normalised so absent has exactly one spelling. */
function echoed(value) {
  return typeof value === 'string' ? value : null
}

/**
 * PATCH /api/memos/{id} -- file a memo into a collection, or take it back out.
 *
 * `collectionId` of null is the unfile, and it is sent as an explicit `null` rather than by
 * omitting the key -- JSON.stringify emits `{"collection_id":null}` for null and drops the
 * key entirely for undefined, and the API reads an absent key as "leave the collection
 * alone". That distinction is load-bearing on this route now that it also takes a title:
 * sending the field is what says the move was meant.
 *
 * @param {string} id
 * @param {?string} collectionId
 * @returns {Promise<object>} The memo in its new state.
 */
export async function patchMemo(id, collectionId) {
  return storedMemo(
    await request(`/api/memos/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ collection_id: collectionId }),
    }),
  )
}

/**
 * PATCH /api/memos/{id} again -- rename a memo.
 *
 * A separate function rather than a second argument to patchMemo, because the two are
 * different operations that happen to share a route, and a combined signature would have to
 * distinguish "no collection given" from "unfile this" in JavaScript -- exactly the
 * absent-versus-null problem the API solves by reading the key's presence. Two functions, two
 * bodies, each naming only the field it means.
 *
 * `title` of null clears it, and the memo then falls back to the first line of its own
 * transcript everywhere it is rendered (memoLabel, and `coalesce` in the API's SQL). That is a
 * real operation: a generated title the owner disagrees with is worth being able to remove
 * rather than only to replace.
 *
 * @param {string} id
 * @param {?string} title
 * @returns {Promise<object>} The memo in its new state.
 */
export async function renameMemo(id, title) {
  return storedMemo(
    await request(`/api/memos/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  )
}

/**
 * DELETE /api/memos/{id} -- remove a memo, its recording and its reminders.
 *
 * Answers with the memo it removed rather than 204, so this returns one for the same reason
 * every other write here does: one shape, reconciled by id. Nothing reads it today beyond
 * logging what went; it is returned rather than discarded because the row is at its last
 * moment of being available and throwing it away here would be the hard part to undo.
 *
 * A 404 means somebody else already deleted it -- a second tab, or a double click that got
 * past the guard. request() turns that into a thrown Error carrying the API's own sentence,
 * which is the right thing to show: the memo really is gone, and the list is about to say so.
 *
 * @param {string} id
 * @returns {Promise<object>} The memo as it was.
 */
export async function deleteMemo(id) {
  return storedMemo(
    await request(`/api/memos/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  )
}

/**
 * POST /api/memos/{id}/retry -- send a failed memo back to the worker (MEMO-17).
 *
 * Answers with the memo, now `queued`, which is the part that matters to the caller rather
 * than a formality: `queued` is non-terminal, so merging this row into a list flips that
 * list's `pending` and restarts the poll. Without the body the card would sit on `failed`
 * until something else happened to refresh it, and the press would look like it did nothing.
 *
 * **A 409 is the interesting failure and it is not an error in the client.** It means the memo
 * is no longer failed -- the worker finished it, or another tab pressed this first -- and the
 * API's sentence names the state it found instead. request() throws it like any other non-2xx,
 * carrying that sentence, which is the right thing to put in front of somebody whose button
 * appeared to do nothing. A 404 is the same shape for a memo that has since been deleted.
 *
 * @param {string} id
 * @returns {Promise<object>} The memo, back in the queue.
 */
export async function retryMemo(id) {
  return storedMemo(
    await request(`/api/memos/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  )
}

/**
 * POST /api/memos/{id}/reminders -- set an alarm or a timer on a memo.
 *
 * @param {string} memoId
 * @param {string} remindAt An absolute instant, ISO 8601. Both of the card's controls
 *   produce one of these: an alarm converts the picked local date and time, and a timer adds
 *   its minutes to the current clock. The API never learns which it was, and does not need
 *   to.
 * @param {?string} note
 * @returns {Promise<object>} The memo, now carrying the reminder.
 */
export async function createReminder(memoId, remindAt, note = null) {
  return storedMemo(
    await request(`/api/memos/${encodeURIComponent(memoId)}/reminders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remind_at: remindAt, note }),
    }),
  )
}

/**
 * GET /api/reminders -- every reminder still owed, soonest first.
 *
 * The one reminder call that does not answer with a memo, because it is the one that is not
 * about a memo the caller is holding. The delivery loop has to know about a reminder set on a
 * memo filed inside a collection nobody has opened -- the fast strip cannot see it -- so this
 * reads across all of them and carries just a label per row rather than the memos themselves.
 *
 * Rows are `{id, memo_id, memo_label, remind_at, note}`. No `delivered_at`: everything here
 * is by definition undelivered, so a column that would be null on every row is not sent.
 *
 * @returns {Promise<Array<object>>}
 */
export async function listPendingReminders() {
  const body = await request('/api/reminders')

  return Array.isArray(body?.reminders) ? body.reminders : []
}

/**
 * PATCH /api/reminders/{id} -- record that a reminder has been shown.
 *
 * No body: the delivery time is `now()` in SQL rather than anything this sends, because a
 * browser's clock has no business writing the column used to judge whether reminders arrive
 * late. Idempotent on the server, which matters here -- this is called from a timer, and a
 * retry after a dropped response must not move the timestamp or 404.
 *
 * @param {string} id
 * @returns {Promise<object>} The memo, with that reminder marked delivered.
 */
export async function acknowledgeReminder(id) {
  return storedMemo(
    await request(`/api/reminders/${encodeURIComponent(id)}`, { method: 'PATCH' }),
  )
}

/**
 * DELETE /api/reminders/{id} -- drop a reminder that was set by mistake.
 *
 * Answers with the memo rather than 204, so the card can re-render from one shape.
 *
 * @param {string} id
 * @returns {Promise<object>} The memo, without that reminder.
 */
export async function deleteReminder(id) {
  return storedMemo(
    await request(`/api/reminders/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  )
}

/**
 * POST /api/memos -- returns the stored row, which is what lets the list show the
 * new memo without a follow-up GET.
 *
 * @param {string} text
 * @returns {Promise<object>}
 */
export async function createMemo(text) {
  return storedMemo(
    await request('/api/memos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),
  )
}

/**
 * POST /api/memos again, as multipart/form-data -- the same route, the same 201, the
 * same row (MEMO-10).
 *
 * One route rather than a /api/memos/audio of its own, because both produce one memo
 * and differ only in which of `transcript` and `audio_path` starts out set. That is
 * what lets the caller prepend the answer to the same list without knowing which kind
 * of memo it just made; api/routes/api.php argues the same thing from its side.
 *
 * **No Content-Type header, deliberately.** fetch sets it from the FormData, and the
 * value it sets carries the multipart boundary -- `multipart/form-data;
 * boundary=----WebKitFormBoundary...`. Setting the header by hand omits the boundary,
 * PHP finds no parts, and $_FILES arrives empty: the request looks to the API exactly
 * like one that forgot to attach anything, and answers 422 "The audio field is
 * required."
 *
 * The third argument to append() is the filename. Without it the part is named `blob`,
 * which is legal and reaches PHP fine -- this is here so a request is legible in a
 * network log. Nothing on the server reads it: the storage key's extension comes from
 * the sniffed bytes, not from this.
 *
 * **XMLHttpRequest rather than fetch, for one reason: upload progress.** fetch has no
 * way to report how much of a request body has gone out -- the streaming request bodies
 * that would allow it are not supported for uploads in Safari or Firefox, and the
 * `duplex` option they need is Chromium-only. XHR has had `upload.onprogress` for
 * fifteen years. Every other call in this file stays on fetch; this is the one that has
 * something to report.
 *
 * On localhost that report is brief to the point of being decorative -- a memo is tens
 * of kilobytes and the bar is gone before it is read. It is kept because the same code
 * runs when the API is not on the same machine, and because the alternative is a
 * *silent* gap between pressing Stop and the row appearing, which is the gap this whole
 * change exists to close.
 *
 * @param {Blob} blob The recording, as MediaRecorder produced it. Whatever container
 *   that is, it is sent unchanged and normalized server-side -- see useRecorder.
 * @param {string} filename
 * @param {?(fraction: number) => void} onProgress Called with 0 to 1 as the body goes
 *   out, and with 1 once it is all sent. Never called for a request whose total size the
 *   browser will not disclose -- see below.
 * @returns {Promise<object>}
 */
export async function createVoiceMemo(blob, filename, onProgress = null) {
  const form = new FormData()

  form.append('audio', blob, filename)

  return storedMemo(await upload('/api/memos', form, onProgress))
}

/**
 * One XHR POST, reporting how much of the body has gone out, and failing exactly the way
 * request() does.
 *
 * The three failure branches below are deliberately the same three, with the same
 * sentences: from where the user is standing a transport failure is a transport failure
 * whether the bytes went out through fetch or through XHR, and two vocabularies for one
 * fault would be a worse outcome than the duplication. request() has the reasoning for
 * each; this only restates them in XHR's vocabulary.
 *
 * No Content-Type is set here either, for the reason createVoiceMemo gives: XHR fills it
 * in from the FormData, boundary included, and setting it by hand omits the boundary and
 * empties $_FILES.
 */
function upload(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()

    xhr.open('POST', path)
    xhr.responseType = 'text'

    if (onProgress) {
      xhr.upload.addEventListener('progress', (event) => {
        // `lengthComputable` is false when the browser will not say how big the body is
        // -- which it does for a chunked or streamed body. Reporting `loaded` alone
        // would be a numerator with no denominator, so the caller is told nothing and
        // renders its indeterminate state instead.
        if (event.lengthComputable && event.total > 0) {
          onProgress(event.loaded / event.total)
        }
      })

      // `load` on the upload object, not on the request: it fires when the last byte is
      // *sent*, which is the moment the wait stops being about the network and starts
      // being about the server. Without it the bar can stall a hair short of full while
      // the API does its work, which is exactly the "is it stuck?" reading this is
      // supposed to prevent.
      xhr.upload.addEventListener('load', () => onProgress(1))
    }

    // Only fires for a transport failure -- never for a 4xx or 5xx, same as fetch.
    xhr.addEventListener('error', () =>
      reject(new Error('Could not reach the app server. Is the stack still running?')),
    )

    xhr.addEventListener('abort', () => reject(new Error('The upload was cancelled.')))

    xhr.addEventListener('load', () => {
      const notFromTheApi = () =>
        new Error(
          `The API did not answer (HTTP ${xhr.status}). Check that the api container is up: docker compose ps`,
        )

      if (!JSON_CONTENT_TYPE.test(xhr.getResponseHeader('content-type') ?? '')) {
        reject(notFromTheApi())

        return
      }

      let body

      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        reject(notFromTheApi())

        return
      }

      if (xhr.status < 200 || xhr.status >= 300) {
        // The one branch of the three that is shared as code rather than restated. The other
        // two are one sentence each and read better written out in XHR's vocabulary; this one
        // now carries a rule -- a 5xx body is never shown, whatever it says (MEMO-17) -- and a
        // rule stated twice is a rule that will be true in one place. A recording is the
        // *most* likely request to hit a 500, since it is the one that writes to a volume.
        reject(new Error(errorMessage(xhr.status, body)))

        return
      }

      resolve(body)
    })

    xhr.send(form)
  })
}

/** The row out of a 201's envelope, or a readable error if the envelope was empty. */
function storedMemo(body) {
  if (!body?.memo) {
    throw new Error('The API accepted the memo but did not return it.')
  }

  return body.memo
}

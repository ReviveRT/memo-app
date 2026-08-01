/*
 * The two calls MEMO-06 exposes, and the only place in this app that knows the
 * wire format.
 *
 * Relative paths on purpose -- `/api/memos`, not an absolute base URL. The dev
 * server proxies /api to the API container (vite.config.js), so same-origin is what
 * makes this app need no CORS handling, no base-URL variable and no build-time
 * configuration at all.
 *
 * Both responses are envelopes, `{"memos": [...]}` and `{"memo": {...}}`, rather
 * than a bare array and a bare row. That was decided for a search that did not exist
 * yet, and the room got used: the list now also carries `query`. Nothing already read
 * here changed type; see api/app/Http/Controllers/MemoController.php.
 */

/** Matches application/json and the +json suffix types, ignoring any charset. */
const JSON_CONTENT_TYPE = /^application\/(?:[\w.+-]+\+)?json\b/i

/**
 * GET /api/memos -- newest first, capped by the API's own default limit of 50, and
 * filtered by `query` when there is one.
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
 * @param {?string} query Null for the unfiltered list. An empty string would be sent as
 *   `?q=` and mean the same thing to the API, but null keeps the parameter off the URL
 *   entirely so the common request has one canonical form.
 * @returns {Promise<{memos: Array<object>, query: ?string}>} The rows, and the filter the
 *   API says they came back for. See useMemos for what the echo is used for.
 */
export async function listMemos(query = null) {
  const path =
    query === null ? '/api/memos' : `/api/memos?${new URLSearchParams({ q: query }).toString()}`

  const body = await request(path)

  return {
    // Defensive because the alternative is a template crash: `v-for` over a
    // non-iterable throws inside the render function, and the stack trace names
    // MemoList rather than the response that caused it.
    memos: Array.isArray(body?.memos) ? body.memos : [],

    // Normalised to null for anything that is not a string, so callers compare against
    // one absent value rather than against null, undefined and a missing key.
    query: typeof body?.query === 'string' ? body.query : null,
  }
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
        reject(
          new Error(
            typeof body?.message === 'string' && body.message !== ''
              ? body.message
              : `The API answered HTTP ${xhr.status}.`,
          ),
        )

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

/**
 * One fetch, one JSON body, and an Error whose message is safe to render.
 *
 * Every failure this can produce ends up in front of the user as `error.message`,
 * so each of the three branches below has to say something a human can act on:
 *
 *   1. fetch() rejected. It only does that for a transport failure -- the dev
 *      server itself is gone -- never for a 4xx or 5xx.
 *   2. The response is not JSON. The API answers JSON for every status including 404
 *      and 500 (shouldRenderJsonWhen in api/bootstrap/app.php), so a non-JSON body
 *      means the answer did not come from the API: it came from the dev server's proxy
 *      failing to reach it. Verified by stopping the api container -- the proxy answers
 *      `502 Bad Gateway`, `Content-Type: text/plain`, zero bytes of body -- and so is
 *      the reason for the branch: calling .json() on that throws `SyntaxError: Failed
 *      to execute 'json' on 'Response': Unexpected end of JSON input`, which reads as a
 *      bug in this file rather than as a container that is down.
 *   3. A JSON error body. Laravel puts the first validation failure in `message`, so a
 *      422 already reads as "The text field is required." and there is no need to walk
 *      the `errors` map to say the same thing.
 *
 *      A 500 is the weak case and deliberately not improved here: with APP_DEBUG off
 *      the API answers `{"message":"Server Error"}` -- checked, by stopping the db
 *      container -- so that is what the user sees, and the detail is on the api
 *      container's stderr where LOG_CHANNEL puts it. MEMO-17 owns failure UX and is
 *      where a better answer belongs; inventing one here would be a second, different
 *      story about the same 500.
 */
async function request(path, init) {
  let response

  try {
    response = await fetch(path, init)
  } catch (cause) {
    throw new Error('Could not reach the app server. Is the stack still running?', { cause })
  }

  const notFromTheApi = () =>
    new Error(
      `The API did not answer (HTTP ${response.status}). Check that the api container is up: docker compose ps`,
    )

  if (!JSON_CONTENT_TYPE.test(response.headers.get('content-type') ?? '')) {
    throw notFromTheApi()
  }

  let body

  try {
    body = await response.json()
  } catch {
    // A body that claims to be JSON can still be truncated -- the api container dying
    // mid-response. That is the same failure as case 2 from where the user is standing,
    // and an unhandled SyntaxError here would instead read as a bug in this file.
    throw notFromTheApi()
  }

  if (!response.ok) {
    throw new Error(
      typeof body?.message === 'string' && body.message !== ''
        ? body.message
        : `The API answered HTTP ${response.status}.`,
    )
  }

  return body
}

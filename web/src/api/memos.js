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
 * than a bare array and a bare row. That is the API's decision, made so MEMO-19 can
 * add what a search matched on without changing the type of anything already read
 * here; see api/app/Http/Controllers/MemoController.php.
 */

/** Matches application/json and the +json suffix types, ignoring any charset. */
const JSON_CONTENT_TYPE = /^application\/(?:[\w.+-]+\+)?json\b/i

/**
 * GET /api/memos -- newest first, capped by the API's own default limit of 50.
 *
 * @returns {Promise<Array<object>>}
 */
export async function listMemos() {
  const body = await request('/api/memos')

  // Defensive because the alternative is a template crash: `v-for` over a
  // non-iterable throws inside the render function, and the stack trace names
  // MemoList rather than the response that caused it.
  return Array.isArray(body?.memos) ? body.memos : []
}

/**
 * POST /api/memos -- returns the stored row, which is what lets the list show the
 * new memo without a follow-up GET.
 *
 * @param {string} text
 * @returns {Promise<object>}
 */
export async function createMemo(text) {
  const body = await request('/api/memos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })

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

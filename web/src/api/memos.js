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

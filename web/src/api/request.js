/*
 * One fetch wrapper, shared by memos.js and collections.js.
 *
 * It lived inside memos.js until there was a second resource to talk to. Copying it would
 * have meant two copies of the reasoning below, and the failure modes it handles are
 * properties of *this stack* -- the Vite proxy, Laravel's JSON error shape -- rather than of
 * either resource, so they cannot legitimately diverge.
 */

/** Matches application/json and the +json suffix types, ignoring any charset. */
export const JSON_CONTENT_TYPE = /^application\/(?:[\w.+-]+\+)?json\b/i

/**
 * One fetch, one JSON body, and an Error whose message is safe to render.
 *
 * Every failure this can produce ends up in front of the user as `error.message`,
 * so each of the branches below has to say something a human can act on:
 *
 *   1. fetch() rejected. It only does that for a transport failure -- the dev
 *      server itself is gone -- never for a 4xx or 5xx.
 *   2. A 204, which is not an error at all and is why it is checked before anything about
 *      content types. `DELETE /api/collections/{id}` answers one, and a 204 carries no body
 *      and no Content-Type -- so without this branch the next check would call it "not from
 *      the API" and a successful delete would be reported as a broken container. Returns
 *      null, which is the one case a caller must not treat as a body.
 *   3. The response is not JSON. The API answers JSON for every status including 404
 *      and 500 (shouldRenderJsonWhen in api/bootstrap/app.php), so a non-JSON body
 *      means the answer did not come from the API: it came from the dev server's proxy
 *      failing to reach it. Verified by stopping the api container -- the proxy answers
 *      `502 Bad Gateway`, `Content-Type: text/plain`, zero bytes of body -- and so is
 *      the reason for the branch: calling .json() on that throws `SyntaxError: Failed
 *      to execute 'json' on 'Response': Unexpected end of JSON input`, which reads as a
 *      bug in this file rather than as a container that is down.
 *   4. A JSON error body. Laravel puts the first validation failure in `message`, so a
 *      422 already reads as "The text field is required." and there is no need to walk
 *      the `errors` map to say the same thing. That is what carries the wording chosen in
 *      the FormRequests -- "The filter field ...", "You already have a collection called
 *      ..." -- straight to the screen, and it is why those messages are written for a
 *      reader rather than for a log.
 *
 *      A 500 is the weak case and deliberately not improved here: with APP_DEBUG off
 *      the API answers `{"message":"Server Error"}` -- checked, by stopping the db
 *      container -- so that is what the user sees, and the detail is on the api
 *      container's stderr where LOG_CHANNEL puts it. MEMO-17 owns failure UX and is
 *      where a better answer belongs; inventing one here would be a second, different
 *      story about the same 500.
 *
 * @returns {Promise<?object>} The decoded body, or null for a 204.
 */
export async function request(path, init) {
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

  // Before the Content-Type test, deliberately -- see case 2 above.
  if (response.status === 204) {
    return null
  }

  if (!JSON_CONTENT_TYPE.test(response.headers.get('content-type') ?? '')) {
    throw notFromTheApi()
  }

  let body

  try {
    body = await response.json()
  } catch {
    // A body that claims to be JSON can still be truncated -- the api container dying
    // mid-response. That is the same failure as case 3 from where the user is standing,
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

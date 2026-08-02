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
 *      A 500 used to be the weak case and is no longer passed through at all: with
 *      APP_DEBUG off the API answers `{"message":"Server Error"}` -- checked, by stopping
 *      the db container -- which says nothing, and with it on the same body carries the
 *      exception and its trace. MEMO-17 owns failure UX and this is the answer it chose;
 *      see errorMessage below for why the 4xx half is still verbatim.
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
    throw new Error(errorMessage(response.status, body))
  }

  return body
}

/**
 * What to put in front of the user for a non-2xx, which is not always what the body says.
 *
 * **The split is at 500, and MEMO-17 is what closed it.** A 4xx message is authored: the
 * FormRequests and the controllers write those sentences for a reader, and passing them
 * through unchanged is the whole reason this file reads `message` at all -- "You already have
 * a collection called Work", "Only a failed memo can be retried, and this one is ready."
 * Rewording those here would throw away the wording chosen next to the rule.
 *
 * A 5xx message is not authored by anybody, and which useless thing it is depends on a
 * setting:
 *
 *   * with `APP_DEBUG=false`, the shipped configuration, Laravel answers
 *     `{"message":"Server Error"}` -- true, and it tells the reader nothing at all, not even
 *     that looking in a log would help.
 *   * with `APP_DEBUG=true`, which .env.example documents as off and a developer may well
 *     turn on, the same body carries the raw exception message alongside `exception`, `file`,
 *     `line` and a full `trace`. Rendering `message` verbatim then puts a PHP internal --
 *     "SQLSTATE[08006] [7] connection to server at ..." -- into a memo card, complete with
 *     whatever the DSN contained.
 *
 * So neither is passed through. This is MEMO-17's rule applied to the API the way the worker
 * applies it to a provider: the detail goes to the log and the row gets a sentence this code
 * wrote (memo_ai/audio.py's `_run` makes the same argument about ffmpeg's stderr). The
 * sentence names the log rather than apologising, because a 5xx is the one failure the person
 * reading it cannot fix from the page they are on -- and on this project that log is one
 * command away.
 *
 * The status is included because it is the one part of a 5xx worth telling apart: a 500 and a
 * 503 send someone to different places, and it is the string they will paste into an issue.
 *
 * Nothing authored is being thrown away by that, checked before widening the rule this far:
 * the one route in this API that answers a *deliberate* 5xx is `GET /api/health`, whose 503 is
 * a report rather than a `message` -- and no browser code calls it. It is the compose
 * healthcheck's, over curl. A health panel added later would want the report, not this.
 *
 * @param {number} status
 * @param {?object} body The decoded error body. Trusted for its `message` on a 4xx only.
 * @returns {string}
 */
export function errorMessage(status, body) {
  if (status >= 500) {
    return `The app server failed to handle that (HTTP ${status}). The reason is in its log: docker compose logs api`
  }

  return typeof body?.message === 'string' && body.message !== ''
    ? body.message
    : `The API answered HTTP ${status}.`
}

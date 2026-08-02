/*
 * POST /api/ask -- the one call in this app that reads its response as it arrives.
 *
 * Every other request goes through `request()` in request.js, which awaits `.json()` and hands
 * back a body. That is exactly what must not happen here: the answer is produced a token at a
 * time by a local model, and waiting for the last one before showing the first is the whole
 * cost this endpoint's streaming exists to avoid. So this file is a second, deliberately
 * separate client, and the duplication is bounded to the two error branches it shares.
 *
 * **Not `EventSource`, despite this being exactly what it is for.** EventSource only issues
 * GET, and the question travels in a body -- a question about your own memos has no business
 * in a URL, a browser history entry or an access log. So `fetch` plus a `ReadableStream`
 * reader, and the wire format is one JSON object per line rather than SSE's framing, which
 * would buy nothing once EventSource is off the table.
 *
 * The API's own reasoning for the same choices is in api/routes/api.php and
 * ai/memo_ai/ask/app.py.
 */

/** The response's media type. Checked, so a proxy error page is not parsed as an answer. */
const NDJSON = /^application\/x-ndjson\b/i

/**
 * Ask a question about the memos, calling back as the answer arrives.
 *
 * **The callbacks are not decoration.** Returning a promise of the finished answer would make
 * this function trivially simpler and would throw away the only thing it is for -- there is
 * nothing to show for twenty seconds and then everything at once, which is the experience
 * without them.
 *
 * @param {string} question
 * @param {object} handlers
 * @param {(sources: Array<object>) => void} handlers.onSources Called once, before any text,
 *   with the memos the answer is being built from. This arrives within milliseconds -- the
 *   retrieval is one Postgres query -- so it is what fills the panel while the model works.
 * @param {(text: string) => void} handlers.onToken Called with each piece of the answer. A
 *   piece is not a word and not a sentence; append it and re-render.
 * @param {(cited: Array<number>) => void} handlers.onDone Called once, with the `ref` numbers
 *   the answer actually referred to. Computed by the API from the whole answer rather than by
 *   this file, because a citation is regularly split across two tokens -- `[` and `1]` -- and
 *   nothing watching the pieces go past would see it.
 * @param {AbortSignal} [handlers.signal] Aborting closes the connection, which is what tells
 *   the model to stop generating. Not merely a saved render: the answer costs CPU on the
 *   machine the browser is running on.
 * @returns {Promise<void>} Resolves when the answer is complete. Rejects with an Error whose
 *   message is safe to render.
 */
export async function askMemos(question, { onSources, onToken, onDone, signal } = {}) {
  let response

  try {
    response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
      signal,
    })
  } catch (cause) {
    // AbortError arrives here too, and it is not a failure -- it is the user pressing Stop or
    // navigating away. Rethrown as-is so the caller can recognise it by name; every other
    // transport failure gets the sentence request.js uses for the same thing.
    if (cause?.name === 'AbortError') {
      throw cause
    }

    throw new Error('Could not reach the app server. Is the stack still running?', { cause })
  }

  if (!response.ok) {
    throw new Error(await failureMessage(response))
  }

  if (!NDJSON.test(response.headers.get('content-type') ?? '')) {
    throw new Error(
      `The API did not answer (HTTP ${response.status}). Check that the api container is up: docker compose ps`,
    )
  }

  // Whether a terminating event arrived. Checked after the loop -- see below.
  let finished = false

  for await (const event of events(response.body)) {
    if (event.type === 'sources') {
      onSources?.(Array.isArray(event.sources) ? event.sources : [])
    } else if (event.type === 'token') {
      if (typeof event.text === 'string') {
        onToken?.(event.text)
      }
    } else if (event.type === 'error') {
      // **A failure after the first byte, which cannot be a status code.** The 200 went out
      // before the model had produced anything, so a generation that gave up halfway arrives
      // here instead. The caller keeps whatever text it already has and shows this beside it.
      throw new Error(
        typeof event.message === 'string' && event.message !== ''
          ? event.message
          : 'The answer stopped before it was finished.',
      )
    } else if (event.type === 'done') {
      finished = true

      onDone?.(Array.isArray(event.cited) ? event.cited : [])
    }
  }

  /*
   * **A stream that simply stopped is a failure, and nothing else would notice it.** Every
   * successful answer ends in `done` and every reported failure ends in `error`, so reaching
   * here without either means the connection was cut partway through -- the api container
   * restarting, ai-api being stopped mid-answer, a proxy giving up. Without this check the
   * caller would see a half-written answer settle and read as complete, which is the one
   * wrong outcome that looks exactly like a right one.
   *
   * The completeness check lives here rather than in PHP deliberately: the proxy passes bytes
   * through and does not author events (App\Contracts\AskBackend), so this is the first place
   * that knows what a finished answer looks like.
   */
  if (!finished) {
    throw new Error('The answer stopped before it was finished. It may be incomplete.')
  }
}

/**
 * What to show for a non-2xx.
 *
 * **A 503 is the one this route really has**, and it is the reason this function exists rather
 * than reusing `errorMessage()` from request.js. That function replaces every 5xx body with a
 * sentence naming `docker compose logs api` -- which is the right rule (MEMO-17: a 5xx body is
 * either Laravel's useless "Server Error" or, with APP_DEBUG on, a stack trace) and the wrong
 * *place* here. Ask is answered by a different container, so the reader is sent to the wrong
 * log and told to read rather than to start something.
 *
 * So the sentence is authored here, on this side, instead of trusting the body. The rule is
 * kept intact: nothing the server said is rendered for any 5xx.
 *
 * A 422 is the other reachable status -- the question was empty, or over the cap -- and it is
 * a 4xx, so its `message` is passed through exactly as request.js passes the rest of this
 * API's authored validation sentences.
 */
async function failureMessage(response) {
  if (response.status === 503) {
    return 'Ask is not available right now. The ai-api service is either not running or still loading its model — check it with: docker compose ps ai-api'
  }

  if (response.status >= 500) {
    return `The app server failed to handle that (HTTP ${response.status}). The reason is in its log: docker compose logs api`
  }

  const body = await response.json().catch(() => null)

  return typeof body?.message === 'string' && body.message !== ''
    ? body.message
    : `The API answered HTTP ${response.status}.`
}

/**
 * Split the body into JSON objects, one per line.
 *
 * **The buffer is the whole of this function and the reason it is not two lines.** A chunk off
 * the network is not a line: it splits wherever TCP and every proxy in between decided, so one
 * chunk can hold three events and the next can hold the second half of a fourth. Parsing each
 * chunk would work in development and fail the moment an answer got long enough to be split.
 *
 * A trailing partial line is kept for the next chunk. Whatever is left when the stream ends is
 * parsed if it is anything at all -- the API terminates every line, so a leftover means the
 * connection was cut mid-event, and a JSON error is the honest outcome rather than silently
 * dropping the last thing said.
 */
async function* events(body) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''

  try {
    for (;;) {
      const { value, done } = await reader.read()

      if (done) {
        break
      }

      buffer += value

      let newline = buffer.indexOf('\n')

      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)

        if (line !== '') {
          yield JSON.parse(line)
        }

        newline = buffer.indexOf('\n')
      }
    }

    if (buffer.trim() !== '') {
      yield JSON.parse(buffer)
    }
  } finally {
    // On every exit path, including the caller aborting or throwing on an `error` event.
    // Releasing the lock lets the body be cancelled, which closes the socket -- and closing
    // the socket is what eventually reaches ai-api and stops it generating.
    reader.releaseLock()
  }
}

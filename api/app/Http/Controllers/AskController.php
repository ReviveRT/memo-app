<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Contracts\AskBackend;
use App\Exceptions\AskUnavailable;
use App\Http\Requests\AskRequest;
use App\Services\Owners\OwnerContext;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\StreamedResponse;

/**
 * POST /api/ask -- the one route in this API that proxies rather than answers (MEMO-24).
 *
 * **Why a proxy at all.** The model lives in the `ai-api` container because it is Python and
 * llama.cpp, and this container is PHP with no AI dependencies in it -- that separation is
 * the same one `ai-worker` has and it is deliberate. What is *not* deliberate anywhere in this
 * project is a second thing the browser talks to: one public surface means CORS is answered
 * once (by not existing), the byte caps and the validation live in one place, and there is one
 * origin to put authentication in front of the day this app has any. So `ai-api` maps no host
 * port and this route is how the browser reaches it.
 *
 * **The whole response is a pass-through and this class never parses it.** See
 * App\Contracts\AskBackend for the argument -- briefly, PHP does not read the memos, choose
 * them, or know what a citation is, and a parser here would be a second copy of the event
 * vocabulary kept in step with memo_ai/ask/service.py by nothing at all.
 *
 * **Two kinds of failure, and only one of them can be a status code.** That is the interesting
 * property of this route and it is a consequence of streaming rather than a shortcut:
 *
 *   * before the first byte -- nothing listening, or ai-api still loading its model -- is a
 *     503 with a sentence, because nothing has been committed yet.
 *   * after it, the 200 has already gone out and cannot be revised. A generation that blows
 *     its deadline halfway through a sentence therefore arrives as an `error` event inside
 *     the NDJSON, and the client shows what it has plus what went wrong.
 *
 * The eagerness in AskBackend::ask is what makes the first bullet reachable at all: everything
 * that can fail early does so before this method returns a response object.
 */
final class AskController extends Controller
{
    /**
     * One JSON object per line. Declared here as well as by ai-api, because this is the
     * response *this* API is promising and a proxy that guessed its own content type from the
     * upstream's would be a proxy that could be told to serve text/html.
     */
    private const NDJSON = 'application/x-ndjson';

    /**
     * OwnerContext is here rather than behind the backend, because "whose memos" is a property
     * of the *request* and the backend is a transport. An implementation that read the owner
     * itself would be one that could be constructed without one.
     */
    public function __construct(
        private readonly AskBackend $backend,
        private readonly OwnerContext $owner,
    ) {}

    /**
     * Extra seconds of `max_execution_time` beyond the read timeout, for the request itself.
     *
     * Small: everything outside the read loop is a validation pass and a connect, and the
     * loop's own bound is `memo.ask.read_timeout`. This is a margin, not a second budget.
     */
    private const EXECUTION_MARGIN = 30;

    public function store(AskRequest $request): StreamedResponse
    {
        // **Without this the answer is a 500 after exactly thirty seconds**, and it was --
        // reproduced before this line existed, against a question whose three retrieved memos
        // took the model longer than that: `PHP Fatal error: Maximum execution time of 30
        // seconds exceeded in guzzlehttp/psr7/src/Stream.php`, with a partial answer already
        // written to the client and a 500 that could no longer be sent.
        //
        // Thirty is PHP's compiled-in default for a web SAPI. No file in api/conf.d/ sets
        // `max_execution_time`, and none should: it is the right guard for every other route
        // in this API, all of which answer in milliseconds. This is the one request that is
        // *supposed* to hold a connection open for a minute, so it raises the limit for
        // itself rather than for everything.
        //
        // Derived from the same config the read timeout comes from, so the two cannot drift
        // into a stack where PHP gives up before Guzzle does -- which would be the worst
        // arrangement of the three, since ai-api's own deadline is what produces a readable
        // `error` event and PHP timing out first replaces it with a truncated stream.
        //
        // Not `0`. An unbounded request is a thread this container cannot get back if
        // something upstream stops talking without closing the socket.
        set_time_limit((int) config('memo.ask.read_timeout') + self::EXECUTION_MARGIN);

        try {
            $chunks = $this->backend->ask($request->question(), $this->owner->current()->id);
        } catch (AskUnavailable $e) {
            // 503 with the exception's own sentence, which is written for a person -- see
            // App\Exceptions\AskUnavailable, including why the browser still does not render
            // it and where the sentence it *does* render is written.
            abort(Response::HTTP_SERVICE_UNAVAILABLE, $e->getMessage());
        }

        return response()->stream(
            static function () use ($chunks): void {
                foreach ($chunks as $chunk) {
                    echo $chunk;

                    // **Both calls, and neither is redundant.** PHP's own output buffer is the
                    // first place a chunk can be held -- Laravel leaves one open under some
                    // configurations and not others, hence the level check rather than an
                    // unconditional ob_flush(), which emits a notice when there is no buffer.
                    // `flush()` is the second: it pushes the SAPI's buffer at the web server.
                    // Without the pair, every token written here would be delivered together
                    // at the end, which is the whole thing streaming exists to prevent.
                    if (ob_get_level() > 0) {
                        ob_flush();
                    }

                    flush();
                }
            },
            Response::HTTP_OK,
            [
                'Content-Type' => self::NDJSON,

                // The same `no-store` GET /api/memos carries, and for a stronger reason: an
                // answer is generated once, for one question, and there is no second reader
                // a cached copy could be correct for.
                'Cache-Control' => 'no-store',

                // For anything that might buffer a proxied response by default. Nothing in
                // this stack does -- Vite's dev server proxy streams, and FrankenPHP writes
                // what the two flushes above hand it -- and the header is one string against
                // the day this sits behind something that would.
                'X-Accel-Buffering' => 'no',
            ],
        );
    }
}

<?php

declare(strict_types=1);

namespace App\Contracts;

use App\Exceptions\AskUnavailable;

/**
 * Whatever answers a question about the memos. One method, and it hands back bytes.
 *
 * The same kind of seam App\Contracts\AudioStorage is, and it exists for the same two
 * reasons. One is the swap point: today the implementation is an HTTP call to the `ai-api`
 * container, and a hosted model or an in-process one would be a different class and one line
 * in AppServiceProvider. The other is the test suite -- `php artisan test` has no ai-api to
 * talk to, and a controller that built its own client could only be tested by starting one.
 *
 * **It yields raw bytes rather than parsed events, and that is the interesting decision in
 * this file.** The API's job on this route is to be the only public surface, not to be a
 * second opinion about the answer: PHP does not read the memos, does not choose them, does
 * not know what a citation is. Parsing the NDJSON here would mean this layer having an
 * opinion about the event vocabulary, which would then have to be kept in step with
 * memo_ai/ask/service.py through no mechanism at all -- and it would have to re-serialise
 * every line, which is work done twice to arrive back where it started.
 *
 * So the proxy is a proxy. Adding an event type to the Python service needs no change on
 * this side, which is the property a pass-through has and a parser does not.
 *
 * The cost of that is worth stating rather than discovering: PHP cannot turn a *late*
 * failure into a status code, because it has already committed to 200 by the time the first
 * chunk arrives. Failures after that point travel inside the stream as an `error` event, and
 * memo_ai/ask/service.py makes the same argument from the other end.
 */
interface AskBackend
{
    /**
     * Ask one question, and hand back the answer stream as it arrives.
     *
     * **Eager, despite returning an iterable.** Whatever it takes to establish that there is
     * something on the other end has to happen before this returns, so that a backend which
     * is down is a 503 rather than an empty 200 -- an implementation whose method body is
     * itself a generator would defer all of it to the first iteration, which is after the
     * response has begun. HttpAskBackend does the request here and returns a generator over
     * the body.
     *
     * @param  string  $ownerId  Whose memos to answer from. Not optional, and not defaulted:
     *                           an implementation that could be called without it would be
     *                           one retrieval away from quoting a stranger's transcript into
     *                           somebody's answer, which is the worst-shaped leak this
     *                           application can produce -- the private text is not merely
     *                           reachable, it is read aloud. The Python side refuses a
     *                           request that omits it for the same reason.
     * @return iterable<string> Chunks of NDJSON, in order, split wherever the network split
     *                          them. A chunk is not a line: a caller writing these straight
     *                          to the client does not care, and one that wanted lines would
     *                          have to buffer.
     *
     * @throws AskUnavailable When there is nothing to ask, or it refused before answering.
     */
    public function ask(string $question, string $ownerId): iterable;
}

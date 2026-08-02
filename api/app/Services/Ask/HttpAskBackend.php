<?php

declare(strict_types=1);

namespace App\Services\Ask;

use App\Contracts\AskBackend;
use App\Exceptions\AskUnavailable;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

/**
 * The `ai-api` container, over the compose network.
 *
 * The only thing in this project that talks to it. It is not on the public network -- no host
 * port is mapped for that service -- so `http://ai-api:8000` resolves inside the compose
 * network and nowhere else, which is what makes `/api/ask` a proxy rather than a redirect.
 */
final class HttpAskBackend implements AskBackend
{
    /** How much of the body to ask for per read. An upper bound, never a wait. */
    private const CHUNK_BYTES = 1024;

    /**
     * The socket's read granularity, in bytes -- and **the one number in this class that
     * decides whether the answer streams at all**.
     *
     * PHP's stream layer fills its buffer `chunk_size` bytes at a time and returns once it
     * has that much, so this is not a performance knob: it is how long a token waits before
     * PHP will hand it over. At the default of 8,192 the whole feature silently stops
     * streaming, which is what it did before this line existed.
     *
     * Measured against the running ai-api, on the same question, timing the first chunk out
     * of Guzzle's body:
     *
     *   chunk_size  first chunk   reads for a 1,827-byte answer
     *   8,192           9.46 s    3
     *   512             0.53 s    4
     *   64              0.06 s    26
     *   1               0.04 s    1,827
     *
     * The 9.46 s row is the bug: nothing reaches the browser until the model has produced
     * about a kilobyte of tokens, so the sources -- which are ready in milliseconds -- arrive
     * with the answer instead of ahead of it, and the whole point of the NDJSON is lost.
     *
     * Eight, which is below the size of the smallest event this stream carries (a token line
     * is around forty bytes), so no event ever waits for the next one to fill a buffer. The
     * cost is a few hundred reads over a whole answer, against twenty-five seconds of
     * inference -- it does not register. One byte would be marginally more direct and buys
     * nothing measurable for eight times the syscalls.
     */
    private const READ_CHUNK_BYTES = 8;

    /**
     * @param  string  $baseUrl  Origin only, no path. `http://ai-api:8000` under compose.
     * @param  int  $connectTimeout  Seconds to establish the connection. Short: the failure
     *                               this bounds is "that service is not running", which
     *                               should be a 503 in front of the user immediately rather
     *                               than after a minute of nothing.
     * @param  int  $readTimeout  Seconds to wait for the *next* piece of the body, not for
     *                            the whole answer. It has to clear the gap before the first
     *                            token, which is the model processing the prompt and is the
     *                            longest gap in the exchange -- so it is set from
     *                            ASK_DEADLINE_SECONDS' order of magnitude rather than from
     *                            how long a token takes.
     */
    public function __construct(
        private readonly string $baseUrl,
        private readonly int $connectTimeout,
        private readonly int $readTimeout,
    ) {}

    /**
     * **No `yield` in this method, and that is deliberate rather than incidental.**
     *
     * A method containing one anywhere is a generator function: none of its body runs until
     * the first iteration. That would move the POST -- and therefore every failure worth a
     * status code -- to *after* the controller had returned a 200 and Symfony had begun
     * sending it. So the request happens here and the generator is a second method.
     *
     * This was the first version of this class and it was wrong in exactly that way: stopping
     * the ai-api container produced an empty 200 rather than a 503, because the exception was
     * thrown inside the response body long after the headers had gone.
     */
    public function ask(string $question): iterable
    {
        return $this->chunks($this->open($question));
    }

    /**
     * Make the request and hand back the socket, still filling.
     *
     * `'stream' => true` is the whole point: without it Guzzle reads the entire response into
     * memory before returning, and an answer that takes forty seconds to generate would arrive
     * all at once at the end. With it, Guzzle routes the request through its StreamHandler and
     * the body is a PHP stream that fills as the bytes come in -- which is also why the
     * timeout below is a per-read one rather than a bound on the whole exchange.
     *
     * **The PSR-7 stream is detached and the raw resource is what gets read**, which is not
     * tidiness: `stream_set_chunk_size` is the only way to stop PHP buffering nine seconds of
     * answer before handing any of it over, and it takes a resource. See READ_CHUNK_BYTES for
     * the measurements. Guzzle's `Stream` is a thin wrapper over exactly this resource and
     * nothing below reads through it again, so there is no second owner to confuse.
     *
     * `acceptJson` is deliberately absent. The response is `application/x-ndjson`, and an
     * `Accept: application/json` would be a request for something this route does not produce.
     *
     * @return resource
     */
    private function open(string $question)
    {
        try {
            $response = Http::withOptions(['stream' => true])
                ->connectTimeout($this->connectTimeout)
                ->timeout($this->readTimeout)
                ->post($this->baseUrl.'/ask', ['question' => $question]);
        } catch (ConnectionException $e) {
            // The container is not running, the name does not resolve, or nothing accepted the
            // connection in time. One sentence for all three, because the reader has the same
            // one thing to do about each -- and none of the three is worth putting a hostname
            // in front of somebody.
            throw new AskUnavailable(
                'Ask is not available: the ai-api service is not answering. '
                    .'Check that it is running: docker compose ps ai-api',
                previous: $e,
            );
        }

        if ($response->successful()) {
            $handle = $response->toPsrResponse()->getBody()->detach();

            if (! is_resource($handle)) {
                // Unreachable with the StreamHandler, which is what `'stream' => true` routes
                // to and which always produces a resource-backed Stream. Guarded rather than
                // assumed, because the alternative is reading from nothing -- an empty body
                // is indistinguishable from a complete answer of no words, which is the one
                // failure on this route that would look like a working feature.
                throw new AskUnavailable(
                    'Ask is not available: the answer could not be read as a stream.'
                );
            }

            stream_set_chunk_size($handle, self::READ_CHUNK_BYTES);

            return $handle;
        }

        // A 503 from ai-api is the ordinary case here and means the model is still loading --
        // it answers that until the weights are in, which is seconds on a warm page cache and
        // is also what a missing or unreadable GGUF looks like. Anything else on this route is
        // a 422 from its own validation, which this class's caller has already made
        // unreachable by validating the same question first.
        //
        // The upstream status is named rather than swallowed, because it is the one detail
        // that tells "still starting" apart from "misconfigured" in a log, and it is not
        // something a person has to interpret to act on.
        throw new AskUnavailable(
            $response->status() === 503
                ? 'Ask is not ready yet: the ai-api service is still loading its model. '
                    .'Try again in a moment.'
                : "Ask is not available: the ai-api service answered {$response->status()}.",
        );
    }

    /**
     * The body, as it arrives.
     *
     * `feof()` is checked before each read rather than trusting an empty read to mean the end,
     * because an empty string from a socket that has not closed is not an end -- it is a read
     * that found nothing yet, and treating it as one would truncate an answer during a pause
     * between tokens.
     *
     * **Which leaves one way to loop forever, and it is handled rather than reasoned away.**
     * A read that hits Guzzle's timeout also returns `''` with `feof()` still false, so a
     * hung ai-api would spin here at the rate of the timeout. It should not be reachable --
     * `read_timeout` is set above ASK_DEADLINE_SECONDS precisely so that ai-api gives up first
     * and says so in an `error` event -- but "should not" is what the metadata check is for.
     *
     * A truncated stream is what the client then sees, with no terminating event. That is
     * deliberate rather than a gap: this class does not author events (see App\Contracts\
     * AskBackend), and web/src/api/ask.js treats a stream that ends without `done` or `error`
     * as a failure, which covers this and every other way the connection can be cut.
     *
     * @param  resource  $handle
     * @return iterable<string>
     */
    private function chunks($handle): iterable
    {
        try {
            while (! feof($handle)) {
                $chunk = fread($handle, self::CHUNK_BYTES);

                if ($chunk === false || stream_get_meta_data($handle)['timed_out']) {
                    Log::warning('Ask: the ai-api response stopped arriving before it ended.');

                    break;
                }

                if ($chunk !== '') {
                    yield $chunk;
                }
            }
        } finally {
            // On every exit path, including the one where the client disconnected and this
            // generator was destroyed mid-answer. Closing the socket is what tells ai-api that
            // nobody is reading -- its own pump then stops generating rather than talking to a
            // closed connection for the rest of its deadline.
            fclose($handle);
        }
    }
}

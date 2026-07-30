<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use JsonException;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpKernel\Exception\BadRequestHttpException;

/**
 * Answers 400 when a request claims to carry JSON and the body does not parse.
 *
 * Without this, a broken body is not an error at all. Request::json() builds its
 * input bag as `new InputBag((array) json_decode($content, true))`, and json_decode
 * returns null on failure, so `(array) null` is `[]` -- an unparseable body becomes
 * an empty input bag and nothing anywhere throws. Validation then reports the fields
 * it did not find, which is how `{"text": ` came back as 422 "The text field is
 * required." The caller's field was present; their JSON was truncated, and the
 * response sent them looking for the wrong thing.
 *
 * This is middleware and not a rule in StoreMemoRequest because the body is broken
 * before any route's expectations apply, and every route that reads one has the same
 * problem: MEMO-11 adds an audio body to POST /api/memos and MEMO-17 adds a retry
 * action. It is also why this cannot live in bootstrap/app.php's withExceptions()
 * block, which is the obvious place to look -- there is no exception to render.
 *
 * 400 rather than 422. 422 says the document was understood and its contents were
 * unacceptable, which is what the FormRequests answer; this one could not be read.
 */
final class ValidateJsonBody
{
    public function handle(Request $request, Closure $next): Response
    {
        // Before the body is touched at all. isJson() reads Content-Type, not Accept,
        // so this only judges a body that was declared to be JSON -- and returning
        // here means a request that was not never has getContent() called on it,
        // which for MEMO-11's audio upload is the difference between ignoring a
        // multipart request and reading megabytes of it into a string to ignore.
        //
        // A JSON body sent without a JSON Content-Type is not checked either, which
        // is deliberate: this holds callers to the format they declared rather than
        // sniffing bodies, and Laravel would not have parsed that one as JSON either.
        if (! $request->isJson()) {
            return $next($request);
        }

        $body = $request->getContent();

        // An empty or whitespace-only body is absence, not corruption, and the honest
        // answer to it is whatever the route's own rules say about the fields that are
        // missing -- "the text field is required" is true when the caller sent no
        // body. This is also exactly what the framework does one layer down:
        // Request::json() substitutes '[]' when trim($content) is empty, so treating
        // it as anything else here would make this middleware and the input bag
        // disagree about the same request. Consistent, too, with App\Support\Env,
        // where a set-but-empty value means unset.
        //
        // strspn rather than the obvious `trim($body) === ''` because trim allocates a
        // whole second copy of the body to answer a question about it. Measured on a
        // 19 MB body -- the largest post_max_size allows -- that copy is a 19 MB spike
        // against this image's 128 MB memory_limit, and FrankenPHP serves concurrent
        // requests from one process, so it is per request in flight rather than one
        // spike at a time. strspn walks the string and allocates nothing. The charlist
        // is PHP's own trim default, and the two were checked to agree across every
        // shape that reaches here, "\0" and "\x0B" included.
        if ($body === '' || strspn($body, " \t\n\r\0\x0B") === strlen($body)) {
            return $next($request);
        }

        // Only whether it parses, not what it parsed to. A bare `5` is well-formed
        // JSON and passes here, becoming [5] in the input bag and then a 422 naming
        // the fields the route wanted -- which is a fair description of a body with no
        // fields in it. This middleware answers "could the body be read"; what a
        // readable body is allowed to contain belongs to the route's own rules.
        //
        // The decoded value is thrown away, so a body that parses is parsed twice --
        // here and again in Request::json(). Left alone rather than primed into the
        // request: the only way to avoid it is to build the input bag by hand and keep
        // that in step with how the framework builds its own, and the cost being
        // avoided is bounded by post_max_size on a request that is about to be
        // rejected on length anyway. Peak for the 19 MB worst case measured at 59 MB
        // against a 128 MB limit.
        try {
            json_decode($body, true, flags: JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            // The decoder's own message ("Syntax error", "Control character error,
            // possibly incorrectly encoded") is included because it describes the
            // caller's own body and nothing of ours -- and because "not valid JSON"
            // on its own sends someone back to guess which part. HttpException
            // messages survive to the client with APP_DEBUG=false, unlike a 500's;
            // Handler::convertExceptionToArray keeps them and replaces everything
            // else with "Server Error".
            throw new BadRequestHttpException(
                "The request body is not valid JSON: {$e->getMessage()}.",
                $e,
            );
        }

        return $next($request);
    }
}

<?php

declare(strict_types=1);

namespace App\Http\Responses;

use Symfony\Component\HttpFoundation\BinaryFileResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;

/**
 * A BinaryFileResponse that frames an unsatisfiable range correctly.
 *
 * **The one thing this adds, and the reason it is a class rather than a line in the
 * controller.** Symfony's prepare() sets `Content-Length` to the size of the whole file
 * before it looks at the `Range` header, and then overwrites it only on the 206 path. A
 * range past the end of the file takes the other branch: the status becomes 416, sendContent()
 * declines to write anything because the response is no longer successful, and the header
 * still promises the entire file. Verified against the running container on symfony/http-
 * foundation v7.4.15 -- a `Range: bytes=999999-1000000` answered `416` with `Content-Length:
 * 24775` and zero bytes of body, and curl reported `transfer closed with 24775 bytes remaining
 * to read` rather than a clean refusal. Caddy does not correct it; the promise goes out on the
 * wire as written and the connection cannot be reused.
 *
 * The controller cannot fix this itself, which is the whole reason this exists: prepare() is
 * called by the framework *after* the action returns, so any Content-Length set in the action
 * is overwritten by the code that introduces the bug. Overriding prepare() is the first point
 * at which the final status is known.
 *
 * A 416 carrying no body and saying so is what RFC 9110 describes: the `Content-Range` Symfony
 * already sets is what tells the client the real size, and the body is optional.
 *
 * Deliberately nothing else. Every other header on a recording -- the type, the disposition,
 * the cache policy -- is set by MemoController::audio, because those are decisions about what
 * this endpoint serves rather than corrections to the framework. This class is a bug fix with
 * a name, and it should stay small enough to delete if the fix lands upstream.
 */
final class AudioFileResponse extends BinaryFileResponse
{
    /**
     * Symfony's Request, not Laravel's, and the difference is a fatal rather than a style
     * point: parameter types are contravariant, so narrowing this to Illuminate\Http\Request
     * -- which is what an import completed to on the first attempt -- makes the declaration
     * incompatible with the parent and PHP refuses to load the class. It shows up as
     * "Premature end of PHP process" in the middle of a test run, naming a test that has
     * nothing to do with it.
     */
    public function prepare(Request $request): static
    {
        parent::prepare($request);

        // Not `!== 200`: a HEAD request also sends no body, and there `Content-Length` must
        // stay at the size a GET would have returned. This is only about the status where the
        // header and the body genuinely disagree.
        if ($this->getStatusCode() === Response::HTTP_REQUESTED_RANGE_NOT_SATISFIABLE) {
            $this->headers->set('Content-Length', '0');
        }

        return $this;
    }
}

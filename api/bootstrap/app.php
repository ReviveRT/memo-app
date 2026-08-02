<?php

declare(strict_types=1);

use App\Console\Commands\PruneOwners;
use App\Http\Middleware\ResolveOwner;
use App\Http\Middleware\ValidateJsonBody;
use App\Http\Requests\StoreMemoRequest;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use Illuminate\Http\Exceptions\PostTooLargeException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        // API routes only. `web:` is not registered and routes/web.php is gone:
        // this container serves JSON to the Vue app in the `web` service and
        // nothing else, so a session, a CSRF token and a Blade view would all be
        // surface with no consumer.
        //
        // apiPrefix defaults to 'api', which is what puts routes/api.php under
        // /api/*. That prefix is load-bearing rather than cosmetic: the web
        // container proxies /api/* to api:8080 unchanged so the browser sees one
        // origin, and the compose healthcheck curls /api/health directly.
        api: __DIR__.'/../routes/api.php',
        commands: __DIR__.'/../routes/console.php',

        // The skeleton's `health: '/up'` is deliberately dropped. It answers 200
        // whenever the framework boots, which is the exact misleading signal this
        // stack must not have: `web` starts on `api: service_healthy`, so a probe
        // that passes while Postgres is unreachable would advertise an API that
        // cannot serve a single memo as ready. /api/health does a real round trip
        // and answers 503 when it fails. Two health endpoints disagreeing is worse
        // than one that tells the truth.
    )
    // Named explicitly rather than left to the skeleton's discovery of
    // app/Console/Commands, which is the same preference the rest of this file shows: what
    // runs is in one readable list instead of inferred from a directory listing. There is
    // one command and it deletes people's memos -- worth being findable by reading this file.
    ->withCommands([PruneOwners::class])
    ->withMiddleware(function (Middleware $middleware): void {
        // A body that claims to be JSON and is not gets a 400 saying so, instead of
        // silently becoming an empty input bag and coming back as somebody else's
        // "field is required". See the middleware for why there is no exception to
        // catch in withExceptions() below.
        //
        // append(), which puts it last in the *global* stack -- ninth, behind
        // ValidatePathEncoding, PreventRequestsDuringMaintenance, ValidatePostSize and
        // the two input transformers. Last is where it belongs: each of those has a
        // more specific answer than "your JSON is broken" and should get to give it
        // first. Maintenance mode is the case where the order decides the answer, and
        // it was checked rather than reasoned about: with the app down, a truncated
        // body answers 503 Service Unavailable, and the same body answers 400 once it
        // is back up. Reversed, a caller would be told to fix their JSON by an
        // application that was not going to read it either way.
        //
        // ValidatePostSize looks like the same argument and is not, which is worth
        // knowing before someone "tightens" this by moving it earlier: a body over
        // post_max_size is discarded by PHP, so it arrives as the empty string, and
        // the empty-body skip in the middleware hands it on to be the 413 it should be
        // no matter which side of ValidatePostSize this sits. Verified from both sides
        // anyway -- 25 MB answers 413, and 19 MB of well-formed JSON gets through to
        // the length rule and answers 422.
        //
        // Global rather than scoped to the api group, which also puts it ahead of
        // routing, so a malformed body answers 400 even on a path that matches no
        // route. That is the intended reading -- the request is unreadable whether or
        // not a route wanted it -- and there is a test pinning it so it stays a
        // decision rather than an accident.
        $middleware->append(ValidateJsonBody::class);

        // Whose memos this request may see. Appended after ValidateJsonBody so that an
        // unreadable body is still a 400 rather than a database round trip first, and
        // global rather than scoped to the api group for the same reason ValidateJsonBody
        // is: there is only one group here, and a route added outside it would silently
        // lose its scoping. That is the one mistake this middleware exists to prevent, so
        // it should not be possible to make by adding a route.
        //
        // App\Services\Owners\OwnerContext is what carries the result to the repositories,
        // and it throws rather than defaulting if this middleware has not run -- so the
        // failure mode of forgetting it is a 500, not one person reading another's memos.
        $middleware->append(ResolveOwner::class);

        // **Required for the owner cookie to be marked Secure on any hosted deployment.**
        // Every free platform terminates TLS at its edge and speaks plain http to the
        // container, so without this Request::isSecure() is false in production and the
        // cookie carrying the bearer token goes out without the one flag that keeps it off
        // plain-http requests. It also fixes the scheme in the claim URL, which is built
        // from the request and would otherwise say http:// on an https deployment.
        //
        // '*' -- trust whatever fronts us -- because the trustworthy proxy's address is not
        // knowable here: it is assigned by the platform, differs per provider, and changes
        // without notice. The usual objection is that a client can then spoof
        // X-Forwarded-Proto, and it is worth being precise about what that buys them here:
        // the headers Laravel reads from this affect the scheme, the host and the client IP.
        // Nothing in this application authorises on IP, and claiming to be https when you
        // are not only sets Secure on your *own* cookie, which stops your own browser from
        // sending it back over http. Both are self-inflicted. The exposure would be real if
        // a route ever trusted the client IP for rate limiting or allow-listing; none does,
        // and one that did should narrow this rather than inherit it.
        $middleware->trustProxies(at: '*');
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        // The framework's own 413, reworded. ValidatePostSize sits in the global stack
        // and throws this when CONTENT_LENGTH exceeds post_max_size -- which is the one
        // oversized upload StoreMemoRequest never sees, because PHP discarded the body
        // before Laravel booted. Its message is "The POST data is too large.", which is
        // shown to somebody who pressed Record and says neither what the limit is nor
        // that a shorter memo would work.
        //
        // Matched on PostTooLargeException specifically rather than on HttpException,
        // which is what keeps this from swallowing the 413s StoreMemoRequest raises with
        // a size in them: those are a plain HttpException for exactly this reason. Both
        // are worded by the same method, so the two paths cannot drift apart.
        //
        // Scoped to the one route that takes a body, because the replacement sentence
        // talks about recordings and this middleware runs on every request -- a large
        // body posted to a path that matches no route would otherwise be told to record a
        // shorter memo. Matched on the path rather than on the route name: ValidatePostSize
        // is global middleware and throws before routing, so there is no route to name yet.
        // Returning null falls through to the framework's own rendering.
        //
        // Status and headers come from the exception rather than being restated, so this
        // cannot answer with a status the thing it is rendering does not have.
        $exceptions->render(function (PostTooLargeException $e, Request $request): ?JsonResponse {
            if (! $request->isMethod('POST') || ! $request->is('api/memos')) {
                return null;
            }

            return response()->json(
                ['message' => StoreMemoRequest::tooLargeMessage((int) config('memo.max_audio_bytes'))],
                $e->getStatusCode(),
                $e->getHeaders(),
            );
        });

        // Unconditionally JSON, not the skeleton's `$request->is('api/*')`.
        // Every route here is already under /api, so the only requests that
        // predicate excludes are the ones matching no route at all -- and those
        // are exactly the ones that would come back as Laravel's HTML error page.
        // The frontend parses every response as JSON, so an HTML 404 surfaces in
        // the browser as an unexplained parse error rather than as a 404.
        $exceptions->shouldRenderJsonWhen(fn (): bool => true);
    })->create();

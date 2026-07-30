<?php

declare(strict_types=1);

use App\Http\Middleware\ValidateJsonBody;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;

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
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        // Unconditionally JSON, not the skeleton's `$request->is('api/*')`.
        // Every route here is already under /api, so the only requests that
        // predicate excludes are the ones matching no route at all -- and those
        // are exactly the ones that would come back as Laravel's HTML error page.
        // The frontend parses every response as JSON, so an HTML 404 surfaces in
        // the browser as an unexplained parse error rather than as a 404.
        $exceptions->shouldRenderJsonWhen(fn (): bool => true);
    })->create();

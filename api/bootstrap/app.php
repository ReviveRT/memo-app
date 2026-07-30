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
        // append(), so it joins the end of the *global* stack: after HandleCors, so a
        // preflight is unaffected, and after ValidatePostSize, so a body over
        // post_max_size is still the 413 it should be rather than a complaint about
        // the truncated JSON that is left. Global rather than scoped to the api
        // group, which puts it before routing and means a malformed body answers 400
        // even on a path that matches no route. That is the intended reading -- the
        // request is unreadable whether or not a route wanted it, the same order
        // ValidatePostSize already applies -- and it is pinned by a test so it stays
        // a decision rather than an accident.
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

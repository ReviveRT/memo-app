<?php

/**
 * Every public route in one file.
 *
 * The /api prefix is part of the path, not stripped by a proxy: the web container
 * forwards /api/* to api:8080 unchanged so the browser sees one origin, and the
 * compose healthcheck curls http://127.0.0.1:8080/api/health directly. A route
 * mounted at /health would answer the second caller and 404 the first.
 */

declare(strict_types=1);

use Memo\Http\Controller\HealthController;
use Slim\App;
use Slim\Routing\RouteCollectorProxy;

return static function (App $app): void {
    // Not a static closure. Slim rebinds a group callable to the container before
    // invoking it, and binding to a static closure is impossible -- which surfaces
    // as "Cannot bind an instance to a static closure" followed by a TypeError
    // from CallableResolver, at boot, for every route in the group.
    $app->group('/api', function (RouteCollectorProxy $api): void {
        $api->get('/health', [HealthController::class, 'show']);

        // POST /api/memos and GET /api/memos land here (MEMO-06).
    });
};

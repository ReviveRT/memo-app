<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\Health\HealthService;
use Illuminate\Http\JsonResponse;
use Symfony\Component\HttpFoundation\Response;

/**
 * The controller layer: HTTP in, HTTP out. No SQL, and no decisions about what
 * healthy means.
 */
final class HealthController extends Controller
{
    public function __construct(private readonly HealthService $health) {}

    public function show(): JsonResponse
    {
        $report = $this->health->check();

        return response()
            ->json(
                $report->toArray(),
                // 503 rather than a 200 carrying "status": "degraded". The compose
                // healthcheck is `curl -fsS /api/health`, which only fails on a
                // non-2xx, and `web` starts on `api: service_healthy` -- so a 200
                // here would mark an API that cannot reach its database as ready to
                // serve traffic. The body still says which check failed, for whoever
                // curls it by hand.
                $report->isHealthy() ? Response::HTTP_OK : Response::HTTP_SERVICE_UNAVAILABLE,
            )
            // A cached health response is worse than none: this endpoint exists to
            // describe the state of the container at the moment it is asked.
            ->header('Cache-Control', 'no-store');
    }
}

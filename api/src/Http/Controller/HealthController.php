<?php

declare(strict_types=1);

namespace Memo\Http\Controller;

use Memo\Http\Json;
use Memo\Service\HealthService;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;

/**
 * The controller layer: HTTP in, HTTP out. No SQL, no decisions about what
 * healthy means.
 */
final class HealthController
{
    public function __construct(private readonly HealthService $health)
    {
    }

    public function show(ServerRequestInterface $request, ResponseInterface $response): ResponseInterface
    {
        $report = $this->health->check();

        // 503 rather than a 200 carrying "status": "degraded". The compose
        // healthcheck is `curl -fsS /api/health`, which only fails on a non-2xx,
        // and `web` starts on `api: service_healthy` -- so a 200 here would mark
        // an API that cannot reach its database as ready to serve traffic. The
        // body still says which check failed, for whoever curls it by hand.
        $response = Json::write($response, $report->toArray(), $report->isHealthy() ? 200 : 503);

        // A cached health response is worse than none: this endpoint exists to
        // describe the state of the container at the moment it is asked.
        return $response->withHeader('Cache-Control', 'no-store');
    }
}

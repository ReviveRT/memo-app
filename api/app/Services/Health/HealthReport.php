<?php

declare(strict_types=1);

namespace App\Services\Health;

/** What GET /api/health answers with. */
final class HealthReport
{
    public function __construct(
        public readonly DatabaseHealth $database,
        public readonly UploadLimits $uploads,
    ) {}

    /**
     * Database connectivity alone decides this. A misconfigured upload limit is
     * reported but not fatal -- the text memo path still works end to end without a
     * single upload, and taking the container out of service over it would stop
     * `web` from ever starting for a fault that affects one route.
     */
    public function isHealthy(): bool
    {
        return $this->database->connected;
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return [
            'status' => $this->isHealthy() ? 'ok' : 'degraded',
            'database' => $this->database->toArray(),
            'uploads' => $this->uploads->toArray(),
        ];
    }
}

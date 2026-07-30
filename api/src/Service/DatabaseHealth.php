<?php

declare(strict_types=1);

namespace Memo\Service;

/** Result of one database probe. */
final class DatabaseHealth
{
    private function __construct(
        public readonly bool $connected,
        public readonly ?string $serverVersion,
        public readonly ?float $latencyMs,
        public readonly ?string $error,
    ) {
    }

    public static function up(?string $serverVersion, float $latencyMs): self
    {
        return new self(true, $serverVersion, round($latencyMs, 2), null);
    }

    public static function down(string $error): self
    {
        return new self(false, null, null, $error);
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return $this->connected
            ? [
                'connected' => true,
                'server_version' => $this->serverVersion,
                'latency_ms' => $this->latencyMs,
            ]
            : [
                'connected' => false,
                'error' => $this->error,
            ];
    }
}

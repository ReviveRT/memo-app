<?php

declare(strict_types=1);

namespace Memo\Support;

use RuntimeException;

/**
 * Every environment variable this container reads, resolved once at boot.
 *
 * Nothing else in the API calls getenv(). A missing DATABASE_URL should fail
 * with one clear message at startup, not as a connection error three layers
 * down, and a reader should be able to see the container's whole input surface
 * in one file.
 */
final class Config
{
    private function __construct(
        public readonly string $databaseUrl,
        public readonly string $audioDir,
        public readonly int $maxAudioBytes,
    ) {
    }

    public static function fromEnvironment(): self
    {
        return new self(
            databaseUrl: self::required('DATABASE_URL'),
            // Defaults mirror docker-compose.yml and .env.example. They are
            // repeated rather than derived because this class also has to work
            // when a service is run outside compose.
            audioDir: self::string('AUDIO_DIR', '/data/audio'),
            maxAudioBytes: self::int('MAX_AUDIO_BYTES', 12 * 1024 * 1024),
        );
    }

    private static function required(string $key): string
    {
        $value = self::lookup($key);

        if ($value === null) {
            throw new RuntimeException(
                "Missing required environment variable {$key}. "
                . 'docker-compose.yml supplies it; a bare `docker run` must pass it explicitly.'
            );
        }

        return $value;
    }

    private static function string(string $key, string $default): string
    {
        return self::lookup($key) ?? $default;
    }

    private static function int(string $key, int $default): int
    {
        $value = self::lookup($key);

        if ($value === null) {
            return $default;
        }

        if (!preg_match('/^\d+$/', $value)) {
            throw new RuntimeException("Environment variable {$key} must be a non-negative integer, got \"{$value}\".");
        }

        return (int) $value;
    }

    /**
     * getenv() first: it reads the real process environment, which is what
     * compose sets. $_ENV and $_SERVER are fallbacks for SAPI configurations
     * whose variables_order omits E, and are only consulted for keys that
     * cannot collide with a CGI variable or an HTTP_* header.
     *
     * An empty string counts as absent, matching the ${VAR:-default} form used
     * throughout docker-compose.yml: a commented-out or blank line in someone's
     * .env produces a set-but-empty variable, and treating that as a real value
     * is how a stack ends up connecting to nothing.
     */
    private static function lookup(string $key): ?string
    {
        foreach ([getenv($key), $_ENV[$key] ?? false, $_SERVER[$key] ?? false] as $candidate) {
            if (is_string($candidate) && $candidate !== '') {
                return $candidate;
            }
        }

        return null;
    }
}

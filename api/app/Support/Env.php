<?php

declare(strict_types=1);

namespace App\Support;

use RuntimeException;

/**
 * env() with the two guarantees config/memo.php needs and Laravel's helper does
 * not give.
 *
 * Both were regressions found by revalidating the port from the previous
 * hand-rolled Config class, and both are silent:
 *
 *  1. An empty string is a value, not an absence. `docker run -e AUDIO_DIR=` --
 *     or a commented-out line in someone's .env -- makes env('AUDIO_DIR',
 *     '/data/audio') return '' rather than the default, because the default only
 *     applies when the variable is unset. LocalAudioStorage would then take ''
 *     as its root and resolve every key to /<key>: audio written to the
 *     filesystem root of the container. Verified before this class existed.
 *
 *     This also matches docker-compose.yml, which uses ${VAR:-default}
 *     throughout precisely so that set-but-empty falls back too.
 *
 *  2. (int) on a non-numeric string is 0, not an error. MAX_AUDIO_BYTES=abc
 *     yields a cap of 0 -- and a zero cap reads as "accepts_max_audio: true" on
 *     /api/health, because every limit is >= 0. A typo in one environment
 *     variable would silently disable the byte check that MEMO-11 depends on.
 */
final class Env
{
    public static function string(string $key, string $default): string
    {
        $value = env($key);

        return is_string($value) && $value !== '' ? $value : $default;
    }

    public static function positiveInt(string $key, int $default): int
    {
        $value = env($key);

        if ($value === null || $value === '' || $value === false) {
            return $default;
        }

        // Rejected rather than coerced. A cap this process cannot understand is a
        // deployment mistake, and the alternative to failing here is enforcing a
        // limit nobody chose.
        if (! is_int($value) && (! is_string($value) || preg_match('/^\d+$/', $value) !== 1)) {
            throw new RuntimeException(
                "Environment variable {$key} must be a non-negative integer, got ".var_export($value, true).'.'
            );
        }

        $value = (int) $value;

        if ($value <= 0) {
            throw new RuntimeException("Environment variable {$key} must be greater than zero, got {$value}.");
        }

        return $value;
    }
}

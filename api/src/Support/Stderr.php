<?php

declare(strict_types=1);

namespace Memo\Support;

/**
 * The container's log sink.
 *
 * Deliberately not PSR-3 and deliberately not Monolog: everything this API logs
 * today is an unhandled throwable or a failed database probe, and both belong on
 * stderr where `docker compose logs api` already looks. When a real logger is
 * needed, bind LoggerInterface in app/dependencies.php and replace the two call
 * sites -- Http\JsonErrorHandler and Service\HealthService.
 *
 * error_log() is avoided on purpose: where it lands depends on the SAPI and on
 * an error_log ini setting this image does not set, so it is a poor place to put
 * the only record of a 500.
 */
final class Stderr
{
    public static function write(string $message): void
    {
        $handle = fopen('php://stderr', 'wb');

        if ($handle === false) {
            return;
        }

        fwrite($handle, rtrim($message, "\n") . "\n");
        fclose($handle);
    }
}

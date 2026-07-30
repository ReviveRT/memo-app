<?php

declare(strict_types=1);

namespace Memo\Database;

use InvalidArgumentException;
use PDO;

/**
 * The one place a PDO handle is created.
 *
 * Connecting is deferred to the first pdo() call rather than done in the
 * constructor, and that is load-bearing rather than a micro-optimisation: the
 * container builds a controller's whole dependency chain before the route
 * handler runs, so an eager connection would turn an unreachable database into a
 * 500 from the error middleware. GET /api/health would then be unable to report
 * the one thing it exists to report. Deferred, the failure surfaces inside the
 * handler where it can be caught and described.
 */
final class ConnectionFactory
{
    /**
     * Must stay under the 3s timeout on the compose healthcheck, or a wedged
     * database makes curl give up before PHP can answer and the container is
     * marked unhealthy with no explanation in the logs. libpq's default is 0,
     * meaning wait indefinitely.
     */
    private const CONNECT_TIMEOUT_SECONDS = 2;

    /** Shows up in pg_stat_activity, which is how you tell API connections from the worker's. */
    private const APPLICATION_NAME = 'memo-api';

    private ?PDO $pdo = null;

    public function __construct(private readonly string $databaseUrl)
    {
    }

    /**
     * Memoised for the lifetime of the request. Not PDO::ATTR_PERSISTENT: a
     * pooled connection outlives the request that set its session state, and
     * this API has no need for one.
     */
    public function pdo(): PDO
    {
        return $this->pdo ??= $this->connect();
    }

    private function connect(): PDO
    {
        [$dsn, $user, $password] = self::parse($this->databaseUrl);

        return new PDO($dsn, $user, $password, [
            // Silent failure modes are the enemy here: without this, a failed
            // statement returns false and the next line works on a null.
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            // Real server-side prepares. Emulation would interpolate values
            // client-side, which both loses the type round-trip Postgres gives
            // us and is the mode every SQL-injection-via-prepared-statement
            // writeup is about.
            PDO::ATTR_EMULATE_PREPARES => false,
            // Integer and boolean columns come back as int and bool, so a row
            // can be json_encode'd straight into a response without a cast pass.
            PDO::ATTR_STRINGIFY_FETCHES => false,
        ]);
    }

    /**
     * DATABASE_URL is a URL; PDO wants a driver DSN plus separate credentials.
     * Handing the URL to PDO directly fails with "invalid data source name",
     * which is a confusing error to get from a connection string that works fine
     * in psql.
     *
     * @return array{0: string, 1: ?string, 2: ?string} DSN, username, password
     */
    public static function parse(string $url): array
    {
        $parts = parse_url($url);

        if ($parts === false || !isset($parts['scheme'], $parts['host'])) {
            throw new InvalidArgumentException(
                'DATABASE_URL is not a valid URL. Expected postgresql://user:password@host:5432/database.'
            );
        }

        // psql and the Python worker both accept either spelling, so this has to
        // as well, or the same string works in two services out of three.
        if (!in_array(strtolower($parts['scheme']), ['postgres', 'postgresql', 'pgsql'], true)) {
            throw new InvalidArgumentException(
                "DATABASE_URL scheme \"{$parts['scheme']}\" is not Postgres. This API speaks pgsql only."
            );
        }

        $database = ltrim($parts['path'] ?? '', '/');

        if ($database === '') {
            throw new InvalidArgumentException('DATABASE_URL has no database name (the path component is empty).');
        }

        $dsn = [
            'host' => $parts['host'],
            'port' => (string) ($parts['port'] ?? 5432),
            'dbname' => $database,
            'connect_timeout' => (string) self::CONNECT_TIMEOUT_SECONDS,
            'application_name' => self::APPLICATION_NAME,
        ];

        // PDO_PGSQL forwards unrecognised DSN keywords to libpq, so sslmode and
        // friends pass through. Allowlisted rather than forwarded wholesale:
        // anything unexpected in the query string should be a loud error here,
        // not a silently ignored connection parameter.
        parse_str($parts['query'] ?? '', $query);

        foreach (['sslmode', 'application_name', 'options'] as $keyword) {
            if (isset($query[$keyword]) && is_string($query[$keyword]) && $query[$keyword] !== '') {
                $dsn[$keyword] = $query[$keyword];
            }
        }

        // The DSN is a semicolon-delimited list with no quoting or escaping, so a
        // value containing one would silently inject a second keyword.
        foreach ($dsn as $keyword => $value) {
            if (str_contains($value, ';')) {
                throw new InvalidArgumentException("DATABASE_URL component \"{$keyword}\" may not contain a semicolon.");
            }
        }

        $pairs = [];

        foreach ($dsn as $keyword => $value) {
            $pairs[] = "{$keyword}={$value}";
        }

        return [
            'pgsql:' . implode(';', $pairs),
            // rawurldecode, because a password with a @ or / in it has to be
            // percent-encoded to survive being written into a URL at all.
            isset($parts['user']) ? rawurldecode($parts['user']) : null,
            isset($parts['pass']) ? rawurldecode($parts['pass']) : null,
        ];
    }
}

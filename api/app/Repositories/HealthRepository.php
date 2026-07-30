<?php

declare(strict_types=1);

namespace App\Repositories;

use Illuminate\Database\DatabaseManager;
use PDO;
use PDOException;

/**
 * The repository layer: SQL lives here, and nothing above this namespace imports
 * PDO or touches the DB facade.
 *
 * DatabaseManager is injected rather than the DB facade used statically, so the
 * dependency is visible in the constructor and a test can substitute it.
 */
final class HealthRepository
{
    public function __construct(private readonly DatabaseManager $db) {}

    /**
     * A real round trip to the server, not a look at the connection object.
     *
     * select() prepares and executes, so this exercises the same PDO
     * prepared-statement path every other repository will use -- meaning a driver
     * that cannot prepare fails here on the healthcheck rather than on a user's
     * first memo. No ORM is involved: Eloquent is not used anywhere in this
     * project, per MEMO-05.
     *
     * @return float Milliseconds. Laravel connects lazily and this is the request's
     *               first use of the connection, so the figure covers TCP setup and
     *               authentication as well as the query -- which is the number worth
     *               watching anyway, since every request pays it.
     *
     * @throws PDOException When the database is unreachable or refuses the query.
     *                      Laravel's QueryException extends PDOException, so both
     *                      the connect-time and query-time failures arrive as one
     *                      type.
     */
    public function ping(): float
    {
        $startedAt = hrtime(true);

        $this->db->connection()->select('select 1');

        // hrtime is monotonic; microtime() would be affected by an NTP step and can
        // report a negative duration.
        return (hrtime(true) - $startedAt) / 1_000_000;
    }

    /**
     * Null rather than throwing when the driver will not say: the version is
     * diagnostic detail, and losing it should not turn a healthy database into a
     * degraded report.
     */
    public function serverVersion(): ?string
    {
        try {
            $version = $this->db->connection()->getPdo()->getAttribute(PDO::ATTR_SERVER_VERSION);
        } catch (PDOException) {
            return null;
        }

        return is_string($version) && $version !== '' ? $version : null;
    }
}

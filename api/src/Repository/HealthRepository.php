<?php

declare(strict_types=1);

namespace Memo\Repository;

use Memo\Database\ConnectionFactory;
use PDO;
use PDOException;

/**
 * The repository layer: SQL and PDO live here, and nothing above this namespace
 * imports PDO.
 */
final class HealthRepository
{
    public function __construct(private readonly ConnectionFactory $connections)
    {
    }

    /**
     * A real round trip to the server, not a look at the connection object.
     *
     * prepare()+execute() rather than query(), even for a constant: with
     * ATTR_EMULATE_PREPARES off this exercises the server-side prepare path that
     * every other repository will use, so a driver that cannot prepare fails here
     * on the healthcheck instead of on a user's first memo.
     *
     * @return float Milliseconds. Connecting is lazy and this is the request's
     *               first use of the handle, so the figure covers TCP setup and
     *               authentication as well as the query -- which is the number
     *               worth watching anyway, since every request pays it.
     *
     * @throws PDOException When the database is unreachable or refuses the query.
     */
    public function ping(): float
    {
        $startedAt = hrtime(true);

        $statement = $this->connections->pdo()->prepare('SELECT 1');
        $statement->execute();
        $statement->fetchColumn();
        $statement->closeCursor();

        // hrtime is monotonic; microtime() would be affected by an NTP step and
        // can report a negative duration.
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
            $version = $this->connections->pdo()->getAttribute(PDO::ATTR_SERVER_VERSION);
        } catch (PDOException) {
            return null;
        }

        return is_string($version) && $version !== '' ? $version : null;
    }
}

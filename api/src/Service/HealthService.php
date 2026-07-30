<?php

declare(strict_types=1);

namespace Memo\Service;

use Memo\Repository\HealthRepository;
use Memo\Support\Config;
use Memo\Support\Stderr;
use PDOException;

/**
 * The service layer: it decides what "healthy" means. The controller only turns
 * that into a status code, and the repository only runs the query.
 */
final class HealthService
{
    public function __construct(
        private readonly HealthRepository $repository,
        private readonly Config $config,
    ) {
    }

    /**
     * Only PDOException is caught, and that is the rule rather than an oversight:
     * a dependency being down is a 503 that names the fault, while this deployment
     * being misconfigured -- an absent or malformed DATABASE_URL, both of which
     * throw before PDO is reached -- is a 500 whose detail belongs in the logs and
     * not in a response any caller can read. Widening this catch would dress a
     * configuration bug up as a transient outage.
     */
    public function check(): HealthReport
    {
        try {
            $latencyMs = $this->repository->ping();
            $database = DatabaseHealth::up($this->repository->serverVersion(), $latencyMs);
        } catch (PDOException $e) {
            // Full detail to the logs, SQLSTATE only to the client. /api/health is
            // reachable through the web container's proxy, and PDO connection
            // messages quote the host, port, role and database name back at you --
            // which is a map of the internal network in the one response an
            // unauthenticated caller is guaranteed to be able to read.
            Stderr::write('[health] database probe failed: ' . $e->getMessage());
            $database = DatabaseHealth::down(self::sqlState($e));
        }

        return new HealthReport($database, UploadLimits::current($this->config->maxAudioBytes));
    }

    /**
     * errorInfo[0], not getCode(). PDO_PGSQL puts libpq's driver code in getCode()
     * -- verified as int 7 for every connection failure -- and the five-character
     * SQLSTATE in errorInfo[0]. Reading getCode() as the SQLSTATE silently yields
     * nothing useful, since it is an int and never matches.
     *
     * Also verified: at connect time libpq collapses every cause into 08006. An
     * unreachable host, a wrong password and a nonexistent database are
     * indistinguishable here, so this code says only "the connection never came
     * up" -- which is why the pointer to the logs is not decoration. A SQLSTATE in
     * another class would mean the connection succeeded and the query itself was
     * refused, and that is a genuinely different fault.
     */
    private static function sqlState(PDOException $e): string
    {
        $sqlState = $e->errorInfo[0] ?? null;

        return is_string($sqlState) && $sqlState !== ''
            ? "unreachable (SQLSTATE {$sqlState}); see the api container logs"
            : 'unreachable; see the api container logs';
    }
}

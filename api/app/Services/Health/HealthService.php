<?php

declare(strict_types=1);

namespace App\Services\Health;

use App\Repositories\HealthRepository;
use Illuminate\Support\Facades\Log;
use PDOException;

/**
 * The service layer: it decides what "healthy" means. The controller only turns
 * that into a status code, and the repository only runs the query.
 */
final class HealthService
{
    public function __construct(
        private readonly HealthRepository $repository,
        private readonly int $maxAudioBytes,
    ) {}

    /**
     * Only PDOException is caught, and that is the rule rather than an oversight: a
     * dependency being down is a 503 that names the fault, while this deployment
     * being misconfigured -- an unparseable DATABASE_URL, a driver that is not
     * installed -- is a 500 whose detail belongs in the logs and not in a response
     * any caller can read. Widening this catch would dress a configuration bug up as
     * a transient outage.
     *
     * Laravel's QueryException extends PDOException, so this covers both a refused
     * query and a connection that never came up.
     */
    public function check(): HealthReport
    {
        try {
            $latencyMs = $this->repository->ping();
            $database = DatabaseHealth::up($this->repository->serverVersion(), $latencyMs);
        } catch (PDOException $e) {
            // Full detail to the log, SQLSTATE only to the client. /api/health is
            // reachable through the web container's proxy, and the driver message
            // quotes the host, port, role and database name back at you -- while
            // Laravel's QueryException appends the SQL and the connection name on
            // top. That is a map of the internal network in the one response an
            // unauthenticated caller is guaranteed to be able to read.
            Log::error('Database health probe failed.', ['exception' => $e]);

            $database = DatabaseHealth::down($this->describe($e));
        }

        return new HealthReport($database, UploadLimits::current($this->maxAudioBytes));
    }

    private function describe(PDOException $e): string
    {
        $sqlState = $this->sqlState($e);

        return $sqlState === null
            ? 'unreachable; see the api container logs'
            : "unreachable (SQLSTATE {$sqlState}); see the api container logs";
    }

    /**
     * errorInfo[0], not getCode(). PDO_PGSQL puts libpq's driver code in getCode()
     * -- verified as int 7 for every connection failure -- and the five-character
     * SQLSTATE in errorInfo[0]. Reading getCode() as the SQLSTATE silently yields
     * nothing useful, since it is an int and never matches.
     *
     * Laravel's QueryException copies errorInfo across from the PDOException it
     * wraps, but only when it actually wraps one, so the previous-exception walk is
     * the fallback rather than dead code.
     *
     * Also verified: at connect time libpq collapses every cause into 08006. An
     * unreachable host, a wrong password and a nonexistent database are
     * indistinguishable here, so this says only "the connection never came up" --
     * which is why the pointer to the logs is not decoration. A SQLSTATE in another
     * class would mean the connection succeeded and the query itself was refused,
     * and that is a genuinely different fault.
     */
    private function sqlState(PDOException $e): ?string
    {
        foreach ([$e, $e->getPrevious()] as $candidate) {
            if (! $candidate instanceof PDOException) {
                continue;
            }

            $sqlState = $candidate->errorInfo[0] ?? null;

            if (is_string($sqlState) && $sqlState !== '') {
                return $sqlState;
            }
        }

        return null;
    }
}

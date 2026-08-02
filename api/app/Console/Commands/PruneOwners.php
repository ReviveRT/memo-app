<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Contracts\AudioStorage;
use App\Exceptions\StorageException;
use App\Repositories\OwnerRepository;
use Illuminate\Console\Command;

/**
 * Delete owners nobody has used in a long time, and everything of theirs.
 *
 * **This exists because an anonymous identity costs nothing to create.** There is no signup
 * to deter anybody and no account to abandon deliberately: a browser that clears its cookies
 * has silently orphaned everything it wrote, and there is no way for the application to learn
 * that. Left alone, `owners` and the memos hanging off it grow with traffic rather than with
 * users -- which on a free Postgres capped near half a gigabyte is the thing that eventually
 * fills it, and the symptom is writes failing for everybody.
 *
 * App\Http\Middleware\ResolveOwner already does the cheaper half of this by refusing to mint
 * an owner for a cookie-less safe read, so crawlers and uptime pingers leave nothing behind.
 * What is left for this command is the real ones: people who used the app and stopped.
 *
 * **An artisan command rather than a SQL script**, which is the obvious alternative given
 * that one statement does the database half. Recordings are the reason: they live outside any
 * foreign key -- on a volume, or in a bucket on a hosted deployment -- so nothing the database
 * deletes can reach them. psql cannot issue a DELETE to object storage; this can, through the
 * same AudioStorage the upload path used, whichever driver that is.
 *
 * Not scheduled by anything in this repository. Free platforms differ too much about how a
 * periodic job is expressed -- a cron service, a scheduled job, an external pinger -- for a
 * schedule here to be right anywhere. deploy/README.md has the per-platform wiring.
 */
final class PruneOwners extends Command
{
    /**
     * The cutoff defaults to 400 days, matching `memo.owner.lifetime_days`, and the agreement
     * is load-bearing rather than tidy: pruning sooner than the cookie expires means a browser
     * presenting a perfectly valid token that no longer resolves. To the person holding it
     * that is indistinguishable from their memos being deleted, because it is.
     *
     * --dry-run because the first thing anybody sensible does with a deletion command is ask
     * what it would delete.
     */
    protected $signature = 'memo:prune-owners
                            {--days= : Delete owners not seen in this many days. Defaults to the cookie lifetime.}
                            {--dry-run : Report what would be deleted and delete nothing.}';

    protected $description = 'Delete owners inactive beyond the cookie lifetime, with their memos and recordings.';

    public function handle(OwnerRepository $owners, AudioStorage $storage): int
    {
        $days = (int) ($this->option('days') ?? config('memo.owner.lifetime_days'));

        if ($days < 1) {
            // Refused rather than clamped. `--days=0` reads as "prune everything inactive",
            // which is every owner including the one who used the app a second ago, and a
            // command that quietly reinterprets that is worse than one that stops.
            $this->error('--days must be at least 1. Refusing to prune every owner.');

            return self::FAILURE;
        }

        $cutoff = gmdate('Y-m-d\TH:i:s\Z', time() - $days * 86_400);

        if ($this->option('dry-run')) {
            // Through a separate read -- prune()'s statement cannot be run dry, because
            // Postgres executes a data-modifying CTE whether or not its output is read.
            //
            // Reporting the counts rather than only the cutoff is the point of the flag. "Would
            // delete owners not seen since 2025-06-28" is true and tells nobody whether that is
            // three abandoned browsers or the entire table, which is the one question somebody
            // runs this to answer.
            $counts = $owners->prunable($cutoff);

            $this->line("Owners not seen since {$cutoff}: {$counts['owners']}.");
            $this->line("Their memos: {$counts['memos']}, of which {$counts['recordings']} have a recording.");
            $this->line('Run without --dry-run to delete them. Collections and reminders go too.');

            return self::SUCCESS;
        }

        // One statement: the owners go, and the memos, collections and reminders go with them
        // by the ON DELETE CASCADE in 007_owners.sql. What comes back is the audio keys those
        // memos referenced, collected in the same statement so there is no window where the
        // rows are gone and the keys are unknown.
        $keys = $owners->prune($cutoff);

        $this->info(sprintf('Pruned owners not seen since %s. %d recording(s) to remove.', $cutoff, count($keys)));

        $removed = 0;
        $failed = 0;

        foreach ($keys as $key) {
            try {
                if ($storage->delete($key)) {
                    $removed++;
                }
            } catch (StorageException $e) {
                // Counted and carried on. A key that cannot be deleted -- a bucket refusing
                // credentials, a volume gone read-only -- must not abort the loop and strand
                // the remaining recordings, and it is not recoverable by retrying here. The
                // database rows are already gone either way, so what is left behind is an
                // orphaned blob: waste, not corruption.
                $failed++;

                $this->warn("Could not delete {$key}: {$e->getMessage()}");
            }
        }

        $this->info("Removed {$removed} recording(s).");

        if ($failed > 0) {
            // A non-zero exit, so a scheduled run that half-worked is visible in whatever ran
            // it rather than only in a log nobody opens. The database half succeeded, which is
            // why this is not FAILURE-with-nothing-done -- but it is not success either.
            $this->warn("{$failed} recording(s) could not be removed and are now orphaned.");

            return self::FAILURE;
        }

        return self::SUCCESS;
    }
}

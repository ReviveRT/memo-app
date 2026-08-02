<?php

declare(strict_types=1);

namespace App\Repositories;

use App\Services\Owners\Owner;
use Illuminate\Database\DatabaseManager;

/**
 * SQL for the owners table. Same rules as every repository here: no ORM, no Eloquent, and
 * the schema belongs to db/migrations rather than to PHP.
 *
 * This is the one repository that does not take an OwnerContext, and cannot: it is what
 * produces the owner the others are scoped by. Every statement below therefore names its
 * own predicate explicitly, and there is no shared owner filter to forget -- the reason
 * each of these queries is safe has to be read off the query itself.
 *
 * Not final, matching MemoRepository, so the feature suite can substitute a fake: every
 * statement here is Postgres-specific and `php artisan test` runs against sqlite in memory.
 */
class OwnerRepository
{
    /**
     * to_char for the same reason every other repository uses it: one fixed, unambiguous
     * wire format instead of whatever the server's DateStyle and the session TimeZone would
     * have produced for PHP to reparse.
     *
     * Aliased `..._iso` rather than to the column's own name, which is the convention
     * MemoRepository::COLUMNS explains at length and which matters wherever an ORDER BY
     * could resolve a bare name against an output label instead of the column. Nothing here
     * orders by it today; the alias is consistent so that adding one later cannot introduce
     * the bug quietly.
     */
    private const COLUMNS = <<<'SQL'
        id,
        to_char(last_seen_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS last_seen_at_iso
        SQL;

    public function __construct(private readonly DatabaseManager $db) {}

    /**
     * The lookup that runs on every request: a hashed cookie to an owner.
     *
     * Takes the hash rather than the token, so no plaintext secret reaches this class or
     * appears in a query log. App\Support\OwnerToken::hash is the only producer.
     *
     * Answers null for a token that is well-formed but belongs to nobody -- an owner pruned
     * for inactivity, a cookie carried over from a database that has since been reset, or a
     * guess. All three are the same thing from here and all three mean "mint a new owner",
     * which is the caller's decision rather than this one's.
     */
    public function findByTokenHash(string $tokenHash): ?Owner
    {
        $rows = $this->db->connection()->select(
            'SELECT '.self::COLUMNS.' FROM owners WHERE token_hash = ? LIMIT 1',
            [$tokenHash],
        );

        return $rows === [] ? null : Owner::fromRow($rows[0]);
    }

    /**
     * A new owner, returned as the row the database actually wrote.
     *
     * RETURNING rather than a follow-up SELECT, for the reason MemoRepository::insert gives:
     * `last_seen_at` is a column default, so without it this would either omit the field or
     * carry a timestamp PHP invented that does not match the table.
     *
     * The id is minted by the caller as a UUIDv7, matching every other table. Note that this
     * is the *internal* id and not the secret -- 007_owners.sql has why those are two columns
     * and why a v7 is correct for one of them and disqualifying for the other.
     */
    public function insert(string $id, string $tokenHash): Owner
    {
        // selectFromWriteConnection, not select(): this is a write that returns rows, and
        // select() is the read path. Same connection today, wrong one the day a read/write
        // split is configured.
        $rows = $this->db->connection()->selectFromWriteConnection(
            'INSERT INTO owners (id, token_hash) VALUES (?, ?) RETURNING '.self::COLUMNS,
            [$id, $tokenHash],
        );

        return Owner::fromRow($rows[0]);
    }

    /**
     * Record that this owner is still around.
     *
     * Called at most once a day per owner rather than on every request -- ResolveOwner owns
     * that decision and has the reasoning. What this method guarantees in exchange is that
     * it is cheap enough to be wrong about occasionally: a single-row update by primary key.
     *
     * The `last_seen_at < ?` predicate is not redundant with the caller's check. Two requests
     * from the same browser can arrive concurrently, both read a stale timestamp and both
     * decide to write; the predicate makes the second one update zero rows instead of
     * queueing behind the first for a row lock. It also means a clock that jumps backwards
     * cannot walk this column backwards with it.
     */
    public function touch(string $id, string $notSeenSince): void
    {
        $this->db->connection()->update(
            'UPDATE owners SET last_seen_at = now() WHERE id = ? AND last_seen_at < ?',
            [$id, $notSeenSince],
        );
    }

    /**
     * How much prune() would delete, without deleting it.
     *
     * A separate statement rather than a flag on prune(), because there is no dry mode of a
     * CTE with a DELETE in it -- Postgres runs a data-modifying CTE to completion whether or
     * not the outer query reads its output.
     *
     * The counts are read in one statement so they describe one snapshot. Split across three
     * queries they could disagree with each other, which on a command whose whole job is to
     * tell somebody what is about to be destroyed is worse than not reporting at all.
     *
     * @return array{owners: int, memos: int, recordings: int}
     */
    public function prunable(string $notSeenSince): array
    {
        $rows = $this->db->connection()->select(
            <<<'SQL'
                WITH doomed AS (
                    SELECT id FROM owners WHERE last_seen_at < ?
                )
                SELECT
                    (SELECT count(*) FROM doomed) AS owners,
                    (SELECT count(*) FROM memos m JOIN doomed d ON d.id = m.owner_id) AS memos,
                    (SELECT count(*) FROM memos m JOIN doomed d ON d.id = m.owner_id
                      WHERE m.audio_path IS NOT NULL) AS recordings
                SQL,
            [$notSeenSince],
        );

        // count(*) is a bigint, which pdo_pgsql hands back as a string rather than an int --
        // the same cast Collection::fromRow explains for memo_count.
        return [
            'owners' => (int) $rows[0]->owners,
            'memos' => (int) $rows[0]->memos,
            'recordings' => (int) $rows[0]->recordings,
        ];
    }

    /**
     * Delete owners not seen since a cutoff, and everything of theirs with them.
     *
     * The memos, collections and reminders go too, by the ON DELETE CASCADE that
     * 007_owners.sql declares on all three -- which is the whole reason this is one statement
     * rather than a traversal. What it does *not* delete is the audio: recordings live in
     * object storage or on a volume, outside any foreign key, so this reports the keys it
     * orphaned and App\Console\Commands\PruneOwners deletes them through AudioStorage --
     * which is the whole reason the prune is a command rather than a SQL script.
     *
     * @return array<int, string> The audio keys belonging to the deleted owners, for the
     *                            caller to remove from storage. Collected in the same
     *                            statement as the delete so there is no window in which the
     *                            rows are gone and the keys are unknown.
     */
    public function prune(string $notSeenSince): array
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            <<<'SQL'
                WITH doomed AS (
                    SELECT id FROM owners WHERE last_seen_at < ?
                ),
                orphaned_audio AS (
                    SELECT m.audio_path
                      FROM memos m
                      JOIN doomed d ON d.id = m.owner_id
                     WHERE m.audio_path IS NOT NULL
                ),
                deleted AS (
                    DELETE FROM owners WHERE id IN (SELECT id FROM doomed)
                )
                SELECT audio_path FROM orphaned_audio
                SQL,
            [$notSeenSince],
        );

        return array_map(static fn (object $row): string => (string) $row->audio_path, $rows);
    }
}

<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;

/**
 * Stands in for MemoRepository so the HTTP contract can be tested without Postgres.
 *
 * `php artisan test` runs on sqlite in memory (phpunit.xml), and every statement in
 * the real repository is Postgres-specific. What this fake buys is the half of
 * MEMO-06 that is not driver-dependent: the validation rules, the default limit,
 * the response shape, and the fact that a text memo is written as
 * source='text' / status='queued' with the typed text as its transcript. What it
 * cannot show is that the SQL is right -- ordering, the generated tsvector column,
 * to_jsonb on a real text[] -- and that is MEMO-25's suite against a real database,
 * not something to fake convincingly here.
 *
 * The parent constructor is deliberately not called: DatabaseManager is the one
 * thing this class exists to avoid, and every method that would have used it is
 * overridden.
 */
final class FakeMemoRepository extends MemoRepository
{
    /** Every insert() call, in order, as the repository received it.
     *
     * @var list<array{id: string, source: string, status: string, transcript: ?string}>
     */
    public array $inserted = [];

    /** What recent() hands back. Set this to whatever the test needs to see rendered.
     *
     * @var list<Memo>
     */
    public array $rows = [];

    /** The limit the last call was made with, which is how the default is asserted. */
    public ?int $lastLimit = null;

    /**
     * The query search() was last called with, or null when the last call was recent().
     *
     * Two separate assertions ride on this: that a blank `?q=` reaches the unfiltered
     * statement rather than a filter matching everything, and that a query arrives
     * trimmed and otherwise exactly as it was typed -- the SQL is what interprets it, and
     * nothing between the query string and the bound parameter may rewrite it.
     */
    public ?string $lastQuery = null;

    /** Whether the last call was search() rather than recent(). */
    public bool $searched = false;

    public function __construct() {}

    public function insert(string $id, string $source, string $status, ?string $transcript): Memo
    {
        $this->inserted[] = [
            'id' => $id,
            'source' => $source,
            'status' => $status,
            'transcript' => $transcript,
        ];

        // Shaped like the row Postgres would have returned: the columns the INSERT
        // sets, plus the defaults. created_at is fixed rather than generated, since a
        // test asserting on a clock it does not control is a test that fails at
        // midnight.
        return new Memo(
            id: $id,
            source: $source,
            status: $status,
            transcript: $transcript,
            title: null,
            summary: null,
            tags: [],
            durationMs: null,
            lastError: null,
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }

    /** @return list<Memo> */
    public function recent(int $limit): array
    {
        $this->lastLimit = $limit;
        $this->lastQuery = null;
        $this->searched = false;

        return $this->rows;
    }

    /**
     * Records the query and returns the same rows.
     *
     * Deliberately not filtering $rows to whatever matches: the filtering is
     * websearch_to_tsquery, a trigram ILIKE and a status pin, and a PHP imitation of
     * those would be a second, wrong definition of what the search does -- passing while
     * the SQL was broken, or failing while it was right. Each arm of that predicate was
     * measured against a live Postgres and the numbers are recorded on
     * MemoRepository::search; what this pins is everything up to it, which is the half
     * that has an HTTP contract. MEMO-25 owns running the statement itself.
     *
     * @return list<Memo>
     */
    public function search(string $query, int $limit): array
    {
        $this->lastLimit = $limit;
        $this->lastQuery = $query;
        $this->searched = true;

        return $this->rows;
    }
}

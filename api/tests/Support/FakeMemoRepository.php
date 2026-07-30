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

    /** The limit recent() was last called with, which is how the default is asserted. */
    public ?int $lastLimit = null;

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

        return $this->rows;
    }
}

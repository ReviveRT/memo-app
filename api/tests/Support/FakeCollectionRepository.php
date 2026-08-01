<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Repositories\CollectionRepository;
use App\Services\Collections\Collection;
use App\Support\TimeWindow;

/**
 * Stands in for CollectionRepository so the HTTP contract can be tested without Postgres.
 *
 * The same seam FakeMemoRepository draws and for the same reason: `php artisan test` runs on
 * sqlite in memory (phpunit.xml), and every statement in the real class is Postgres-specific
 * -- jsonb_agg with an ordered aggregate over a derived table, an aliased `INSERT INTO ... AS
 * c` so one projection serves three statements, and a unique violation on an expression index.
 *
 * What this cannot show is that the SQL is right, and in particular it cannot show the thing
 * this repository is most interesting for: that searching matches the collection's name *or*
 * any memo filed inside it. That was verified against a live Postgres -- a collection named
 * "Ideas" is found by a word that appears only in a memo inside it, and not found by a word
 * that appears nowhere. MEMO-25 owns running those statements in CI.
 *
 * The parent constructor is deliberately not called: DatabaseManager is the one thing this
 * class exists to avoid, and every method that would have used it is overridden.
 */
final class FakeCollectionRepository extends CollectionRepository
{
    /** What list() hands back. @var list<Collection> */
    public array $rows = [];

    /** The arguments list() was last called with, or null. @var ?array{0: ?string, 1: TimeWindow, 2: int} */
    public ?array $lastList = null;

    /** Every insert() call, as `[id, name]`. @var list<array{0: string, 1: string}> */
    public array $inserted = [];

    /** Every rename() call, as `[id, name]`. @var list<array{0: string, 1: string}> */
    public array $renamed = [];

    /** Every delete() call. @var list<string> */
    public array $deleted = [];

    /**
     * What insert() and rename() answer with.
     *
     * `false` is the duplicate-name case the controller turns into a 422, and `null` is
     * rename()'s "no such collection" 404. Those two meaning different things is the invariant
     * the real class documents, so the fake has to be able to produce both.
     */
    public Collection|false|null $writeResult = null;

    /** What delete() answers -- false is the 404. */
    public bool $deleteResult = true;

    public function __construct() {}

    /** @return list<Collection> */
    public function list(?string $text, TimeWindow $window, int $limit): array
    {
        $this->lastList = [$text, $window, $limit];

        // Not filtered to whatever matches, for the reason FakeMemoRepository gives about the
        // memo list: a PHP imitation of an ILIKE plus an EXISTS over a tsvector would be a
        // second, wrong definition of what the search does.
        return $this->rows;
    }

    public function insert(string $id, string $name): Collection|false
    {
        $this->inserted[] = [$id, $name];

        return $this->writeResult === null ? false : $this->writeResult;
    }

    public function rename(string $id, string $name): Collection|false|null
    {
        $this->renamed[] = [$id, $name];

        return $this->writeResult;
    }

    public function delete(string $id): bool
    {
        $this->deleted[] = $id;

        return $this->deleteResult;
    }
}

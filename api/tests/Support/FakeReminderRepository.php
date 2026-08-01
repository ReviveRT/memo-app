<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Repositories\ReminderRepository;

/**
 * Stands in for ReminderRepository so the reminder routes can be tested without Postgres.
 *
 * The same seam the other two fakes draw. What it cannot show is the one behaviour that is
 * pure SQL and easy to get wrong -- that `SET delivered_at = coalesce(delivered_at, now())`
 * makes acknowledging idempotent rather than moving the timestamp on a retry. That was
 * verified against a live Postgres: two PATCHes to the same reminder answered the same
 * `delivered_at`, where `SET delivered_at = now()` would have moved it.
 */
final class FakeReminderRepository extends ReminderRepository
{
    /** What pending() hands back. @var list<object> */
    public array $pendingRows = [];

    /** Every insert() call, as `[id, memoId, remindAt, note]`. @var list<array<int, ?string>> */
    public array $inserted = [];

    /** Whether insert() reports that the memo exists. False is the controller's 404. */
    public bool $memoExists = true;

    /** Every markDelivered() and delete() call. @var list<string> */
    public array $delivered = [];

    public array $removed = [];

    /** The memo id those two answer with, or null for "no such reminder" (a 404). */
    public ?string $memoId = null;

    public function __construct() {}

    /** @return list<object> */
    public function pending(int $limit): array
    {
        return $this->pendingRows;
    }

    public function insert(string $id, string $memoId, string $remindAt, ?string $note): bool
    {
        $this->inserted[] = [$id, $memoId, $remindAt, $note];

        return $this->memoExists;
    }

    public function markDelivered(string $id): ?string
    {
        $this->delivered[] = $id;

        return $this->memoId;
    }

    public function delete(string $id): ?string
    {
        $this->removed[] = $id;

        return $this->memoId;
    }
}

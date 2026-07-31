<?php

declare(strict_types=1);

namespace App\Services\Memos;

use App\Repositories\MemoRepository;
use Illuminate\Support\Str;

/**
 * What a memo is when it is created, and what the list means. The controller turns
 * that into HTTP and the repository turns it into SQL; neither decides it.
 */
final class MemoService
{
    public function __construct(private readonly MemoRepository $repository) {}

    /**
     * The text path: the typed text *is* the transcript, so this row skips
     * transcription and reaches the worker owing only enrichment.
     *
     * The id is a UUIDv7 minted here rather than a column default, and it buys two
     * separate things. The 201 response carries the id without a second round trip,
     * which is what lets MEMO-18's frontend insert the row optimistically and then
     * reconcile it by id when the poll catches up. And v7 is time-ordered, so the
     * primary key agrees with created_at -- inserts land at the right-hand edge of
     * the index instead of scattering across it the way v4 would, and the worker's
     * `ORDER BY created_at` claim has a stable tiebreaker for free.
     *
     * Str::uuid7() rather than Ramsey's Uuid::uuid7() directly: identical output,
     * and it is the seam Laravel's own faking helpers hook into.
     */
    public function createFromText(string $text): Memo
    {
        return $this->repository->insert(
            id: Str::uuid7()->toString(),
            source: Memo::SOURCE_TEXT,
            status: Memo::STATUS_QUEUED,
            transcript: $text,
        );
    }

    /**
     * Newest first, capped, and filtered when there is something to filter by.
     *
     * Unpaginated by design rather than by omission. MEMO-18 polls this route and
     * replaces the page keyed by id, and the search below filters it -- neither wants a
     * cursor, and a `since` parameter is specifically ruled out because a timestamp
     * cannot serve as one. So `limit` is the whole of the contract, and the cap on it
     * lives in ListMemosRequest.
     *
     * The branch is here rather than in the controller because "an empty filter is no
     * filter" is a statement about what the list means, and the two arms are genuinely
     * different queries -- the unfiltered one is an index scan straight down
     * memos_created_idx, and putting a filter in front of it would cost that plan for
     * every request that had nothing to filter by. ListMemosRequest::searchQuery has
     * already collapsed a missing `q` and a blank one into null, so null is the only
     * spelling of "unfiltered" that reaches here.
     *
     * @param  ?string  $query  Already trimmed, non-empty, and capped at
     *                          ListMemosRequest::MAX_QUERY_LENGTH.
     * @return list<Memo>
     */
    public function recent(?string $query, int $limit): array
    {
        return $query === null
            ? $this->repository->recent($limit)
            : $this->repository->search($query, $limit);
    }
}

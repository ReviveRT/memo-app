<?php

declare(strict_types=1);

namespace App\Services\Collections;

use App\Repositories\CollectionRepository;
use App\Support\TimeWindow;
use Illuminate\Support\Str;

/**
 * What a collection is, and what the grid means. The controller turns that into HTTP and
 * the repository turns it into SQL; neither decides it.
 *
 * The same shape MemoService has, and thin for the same reason: the decisions a collection
 * involves are mostly about SQL (what a card has to say, what "matches" means) or about
 * HTTP (which failure is a 404 and which a 422), so this layer is the seam rather than the
 * logic. What it does own is the id.
 */
final class CollectionService
{
    public function __construct(private readonly CollectionRepository $repository) {}

    /**
     * The grid, newest first.
     *
     * A pass-through, and unlike MemoService::list there was never a branch here to lose:
     * the repository has always assembled this statement from optional predicates, because
     * the collection list arrived with a text filter and a date window on day one.
     *
     * @param  ?string  $text  Already trimmed and non-empty, or null. Matches the name or any
     *                         memo inside the collection -- see the repository for why both.
     * @return list<Collection>
     */
    public function list(?string $text, TimeWindow $window, int $limit): array
    {
        return $this->repository->list($text, $window, $limit);
    }

    /**
     * Create a collection with the name the user typed.
     *
     * The id is a UUIDv7 minted here rather than a column default, for the two reasons
     * MemoService::createFromText gives about a memo's: the 201 carries the id without a
     * second round trip -- which is what lets the frontend prepend the new card to the grid
     * it is already showing and then file a memo into it immediately -- and v7 is
     * time-ordered, so the primary key agrees with created_at and inserts land at the
     * right-hand edge of the index.
     *
     * Str::uuid7() rather than Ramsey's Uuid::uuid7() directly: identical output, and it is
     * the seam Laravel's own faking helpers hook into.
     *
     * @return Collection|false False when the name is already taken. See the repository:
     *                          the unique index is the check, so this cannot be raced.
     */
    public function create(string $name): Collection|false
    {
        return $this->repository->insert(Str::uuid7()->toString(), $name);
    }

    /**
     * Rename one collection.
     *
     * Worth having at all, rather than making a name permanent: the name is the only thing
     * that distinguishes one card from another, and it is typed in one go at the moment the
     * collection is created -- which is exactly when the user knows least about what will end
     * up in it. A folder you cannot rename gets abandoned and replaced.
     *
     * @return Collection|false|null False when the new name is taken, null when there is no
     *                               such collection. The controller answers 422 and 404
     *                               respectively; collapsing them would tell somebody
     *                               renaming a collection that it does not exist.
     */
    public function rename(string $id, string $name): Collection|false|null
    {
        return $this->repository->rename($id, $name);
    }

    /**
     * Delete one collection. Its memos survive as fast memos.
     *
     * That the memos survive is not implemented here and deliberately so -- it is the
     * `ON DELETE SET NULL` on `memos.collection_id`, so it holds for anything that ever
     * deletes one of these rows rather than only for this method. 003's comment on the
     * constraint has the argument against CASCADE, which would have destroyed the
     * transcripts along with the folder.
     *
     * @return bool Whether there was a collection to delete. False is the controller's 404.
     */
    public function delete(string $id): bool
    {
        return $this->repository->delete($id);
    }
}

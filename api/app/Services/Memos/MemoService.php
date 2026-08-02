<?php

declare(strict_types=1);

namespace App\Services\Memos;

use App\Contracts\AudioStorage;
use App\Exceptions\StorageException;
use App\Repositories\MemoRepository;
use Illuminate\Support\Str;

/**
 * What a memo is when it is created, and what the list means. The controller turns
 * that into HTTP and the repository turns it into SQL; neither decides it.
 */
final class MemoService
{
    public function __construct(
        private readonly MemoRepository $repository,
        private readonly AudioStorage $audio,
    ) {}

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
     * The voice path: the bytes are stored first, then the row that points at them.
     *
     * That order is the whole of this method's correctness and it is not an
     * optimisation to be tidied away. The worker claims `queued` rows and opens
     * whatever `audio_path` names (memo_ai/pipeline.py), so between the INSERT and the
     * blob landing there is a row promising a file that is not there -- and both
     * replicas poll about once a second, which is far inside the time a 5 MB write
     * takes. AudioStorage::putFile covers the other half: it fsyncs and renames, so the
     * key is never briefly a partial file, which is the failure a reader cannot detect.
     *
     * A write that succeeds and an INSERT that then fails leaves an orphan blob, and
     * that is deliberately not compensated with a delete() in a catch. The two failures
     * are not symmetric: an orphan blob is bytes nothing references, reclaimable by a
     * sweep at any later time, while a row whose file has been deleted is a memo that
     * reaches the user as `failed` and can never be anything else. And the case that
     * makes the catch actively wrong is the ordinary one for a database error -- a
     * connection lost *after* Postgres committed, where the INSERT threw here and the
     * row exists. MEMO-11 states the same preference from the write side.
     *
     * The key is `{id}.{ext}` -- flat, one segment. LocalAudioStorage handles nested
     * keys (SharedAudioVolumeTest pins the directory modes three levels down), so
     * date-sharding is available if this ever holds enough files to want it, and it
     * would buy nothing today: the id is a UUIDv7, so a plain `ls` of the volume is
     * already in recording order, and the id is what a row and its blob are matched by
     * when something has gone wrong.
     */
    public function createFromAudio(AudioUpload $audio): Memo
    {
        $id = Str::uuid7()->toString();
        $key = "{$id}.{$audio->extension}";

        $this->audio->putFile($key, $audio->path);

        return $this->repository->insert(
            id: $id,
            source: Memo::SOURCE_VOICE,
            status: Memo::STATUS_QUEUED,

            // NULL, which is what tells the worker this row owes a transcription.
            // Nothing else distinguishes the two paths for it -- see
            // transcribe_if_owed in memo_ai/pipeline.py, and the `source` column
            // exists for reporting rather than for that branch.
            transcript: null,

            audioPath: $key,
            audioMime: $audio->mimeType,
        );
    }

    /**
     * Newest first, capped, and narrowed by whichever filters the request carried.
     *
     * Unpaginated by design rather than by omission. MEMO-18 polls this route and
     * replaces the page keyed by id -- it does not want a cursor, and a `since` parameter
     * is specifically ruled out because a timestamp cannot serve as one (NOTES.md). So
     * `limit` is the whole of the contract, and the cap on it lives in ListMemosRequest.
     *
     * A pass-through, which it did not use to be: this method held the branch between an
     * unfiltered `recent()` and a filtered `search()`, because those were two statements
     * with two plans and choosing between them was a statement about what the list means.
     * Four independently optional filters make that branch eight-way, so the repository
     * assembles one statement from predicates instead -- and with no filters set it
     * assembles exactly the old unfiltered query, so nothing was traded away for that.
     *
     * What is left here is the seam itself. It stays rather than having the controller
     * call the repository directly, because the controller's job is HTTP and MemoQuery is
     * built from a validated request on one side of this line and turned into SQL on the
     * other.
     *
     * @return list<Memo>
     */
    public function list(MemoQuery $query): array
    {
        return $this->repository->list($query);
    }

    /**
     * File a memo into a collection, or return it to the fast strip with null.
     *
     * Null is a first-class argument rather than an absent one, because "take this out of
     * the collection" is a thing the user does -- a memo filed by mistake has to be able
     * to come back -- and it is the same operation as filing it somewhere else.
     *
     * Returns null when the memo does not exist *or* when the collection does not, which
     * the controller answers as one 404. The repository has the argument for why those two
     * are not worth telling apart: both are the client naming something that is not there,
     * and neither is a fault of the server's.
     *
     * No check that the collection exists before the UPDATE. The foreign key is the check,
     * and a SELECT first would be a race as well as a round trip -- the collection could be
     * deleted in between, which puts us back at handling the violation anyway.
     */
    public function moveToCollection(string $memoId, ?string $collectionId): ?Memo
    {
        return $this->repository->moveToCollection($memoId, $collectionId);
    }

    /**
     * Rename a memo, or clear the name with null.
     *
     * The one field of a memo a client may write. `title` is generated -- the worker cuts a
     * fallback from the transcript and the enrichment pass replaces it with something
     * shorter -- so it is a guess, and a guess the owner disagrees with is what makes a memo
     * unfindable in a strip of thirty. Everything else on the row is either a record of what
     * was said or the queue's own bookkeeping, and neither is the client's.
     *
     * Trimmed, and an empty result becomes null rather than an empty string, so there is one
     * spelling of "this memo has no title of its own" for every reader to test. Postgres
     * would happily store `''`, and `coalesce(title, ...)` -- which is how the collection
     * cards and the reminder labels pick a label -- does not treat it as absent, so a blank
     * title would render as a blank card label rather than falling back to the transcript.
     * UpdateMemoRequest normalises on its own side too; that is agreement rather than
     * reliance, and this is the side the database is on.
     */
    public function rename(string $memoId, ?string $title): ?Memo
    {
        $trimmed = $title === null ? null : trim($title);

        return $this->repository->rename($memoId, $trimmed === '' ? null : $trimmed);
    }

    /**
     * Delete a memo and the recording behind it.
     *
     * **The row goes first and the blob second, which is the reverse of how one is created**
     * -- and both orders are chosen against the same failure. createFromAudio writes the blob
     * before the row, so a worker claiming the row always finds a file. Deleting has to
     * unwind that: while the row exists, something may still be about to read the file, so
     * removing the file first would produce a memo that transcribes as "the audio file for
     * this memo is missing". Once the row is gone nothing can reach the blob at all, and what
     * is left is at worst an orphan.
     *
     * **An orphan blob is the accepted failure here, deliberately.** If the unlink fails --
     * a read-only volume, a permission the container lost -- the memo is still deleted and
     * this still reports success, because the user asked for the memo to go and it has. The
     * alternative is a 500 for a request that already succeeded, and a client that then
     * shows the memo as still present when the database says otherwise. The bytes are
     * reclaimable by a sweep at any later time; the wrong answer is not.
     *
     * StorageException is caught for that reason and not swallowed silently -- it is
     * reported through the same channel every other server-side fault uses, so a volume that
     * has stopped accepting deletes shows up in the log rather than only in `du`.
     *
     * Reminders go with the memo through `ON DELETE CASCADE`, inside Postgres. Nothing here
     * has to know they existed.
     *
     * @return ?Memo The memo as it was, or null when there was no such memo.
     */
    public function delete(string $memoId): ?Memo
    {
        $deleted = $this->repository->delete($memoId);

        if ($deleted === null) {
            return null;
        }

        if ($deleted->audioPath !== null) {
            try {
                $this->audio->delete($deleted->audioPath);
            } catch (StorageException $e) {
                report($e);
            }
        }

        return $deleted->memo;
    }
}

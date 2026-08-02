<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Repositories\MemoRepository;
use App\Services\Memos\DeletedMemo;
use App\Services\Memos\Memo;
use App\Services\Memos\MemoQuery;

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
     * @var list<array{id: string, source: string, status: string, transcript: ?string,
     *                 language: ?string, audio_path: ?string, audio_mime: ?string}>
     */
    public array $inserted = [];

    /** What list() and find() hand back. Set this to whatever the test needs to see rendered.
     *
     * @var list<Memo>
     */
    public array $rows = [];

    /**
     * The MemoQuery list() was last called with, or null if it has not been called.
     *
     * One recorded object rather than the four separate fields this fake used to keep
     * (`lastLimit`, `lastQuery`, `searched`), and the collapse is the point: the repository no
     * longer has two methods to distinguish between, so "was this a search?" is now a question
     * about `$lastQuery->text` rather than about which method ran. Keeping the old flags would
     * have meant a fake asserting on a distinction the real class stopped making.
     *
     * Several assertions ride on this: that a blank `?q=` arrives as null text rather than as
     * a filter matching everything, that a query arrives trimmed and otherwise exactly as
     * typed, that `?collection=none` becomes `unfiledOnly` rather than a null collection id,
     * and that an absent `?limit=` becomes ListMemosRequest::DEFAULT_LIMIT.
     */
    public ?MemoQuery $lastQuery = null;

    /** Every moveToCollection() call, in order, as `[memoId, collectionId]`.
     *
     * @var list<array{0: string, 1: ?string}>
     */
    public array $moved = [];

    /**
     * What moveToCollection() answers with. Null is the "no such memo or collection" case the
     * controller turns into a 404, so the default has to be overridden by any test expecting
     * a successful move.
     */
    public ?Memo $moveResult = null;

    /** Every rename() call, in order, as `[memoId, title]`.
     *
     * @var list<array{0: string, 1: ?string}>
     */
    public array $renamed = [];

    /** What rename() answers with. Null is the "no such memo" case the controller 404s. */
    public ?Memo $renameResult = null;

    /** Every delete() call, in order.
     *
     * @var list<string>
     */
    public array $deleted = [];

    /**
     * The audio key each memo carries, keyed by memo id.
     *
     * Only delete() reads it. `audio_path` is not on Memo -- see DeletedMemo for why it stays
     * off the wire -- so a test that wants to assert the blob was unlinked has to say here
     * which blob the row pointed at.
     *
     * @var array<string, string>
     */
    public array $audioPaths = [];

    public function __construct() {}

    public function insert(
        string $id,
        string $source,
        string $status,
        ?string $transcript,
        ?string $audioPath = null,
        ?string $audioMime = null,
        ?string $language = null,
    ): Memo {
        $this->inserted[] = [
            'id' => $id,
            'source' => $source,
            'status' => $status,
            'transcript' => $transcript,
            'language' => $language,

            // Recorded under the column names rather than the parameter names, because
            // what the assertions are about is the row -- `audio_path` holds a storage
            // key and `audio_mime` describes it, and those are the two names the worker
            // and MEMO-23 read them back under.
            'audio_path' => $audioPath,
            'audio_mime' => $audioMime,
        ];

        // Shaped like the row Postgres would have returned: the columns the INSERT
        // sets, plus the defaults. created_at is fixed rather than generated, since a
        // test asserting on a clock it does not control is a test that fails at
        // midnight.
        //
        // `collectionId` and `reminders` are left at their defaults, which is what the real
        // INSERT produces: every memo is created unfiled and with no reminders, and neither
        // is a parameter of insert() -- see the repository for why filing is a separate
        // operation.
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
            lastErrorCode: null,
            language: $language,
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }

    /**
     * Records the query and returns the rows the test set, whatever was asked for.
     *
     * Deliberately not filtering $rows to whatever matches. The filtering is
     * websearch_to_tsquery, a trigram ILIKE, a status pin, two timestamp comparisons and a
     * nullable-column test, and a PHP imitation of those would be a second, wrong definition
     * of what the list does -- passing while the SQL was broken, or failing while it was
     * right. Each arm was measured against a live Postgres and the numbers are recorded on
     * MemoRepository::list; what this pins is everything up to it, which is the half that has
     * an HTTP contract. MEMO-25 owns running the statement itself.
     *
     * @return list<Memo>
     */
    public function list(MemoQuery $query): array
    {
        $this->lastQuery = $query;

        return $this->rows;
    }

    /**
     * The first row whose id matches, or null.
     *
     * This one *does* imitate the real query, because unlike list() there is nothing
     * driver-specific to get wrong: "the row with this id" means the same thing in every
     * database, and the reminder routes need it to answer with a memo at all.
     */
    public function find(string $id): ?Memo
    {
        foreach ($this->rows as $memo) {
            if ($memo->id === $id) {
                return $memo;
            }
        }

        return null;
    }

    public function moveToCollection(string $memoId, ?string $collectionId): ?Memo
    {
        $this->moved[] = [$memoId, $collectionId];

        return $this->moveResult;
    }

    public function rename(string $memoId, ?string $title): ?Memo
    {
        $this->renamed[] = [$memoId, $title];

        return $this->renameResult;
    }

    /**
     * Imitates the real guard rather than answering from a stored result, and rewrites `$rows`.
     *
     * The same call this fake's find() makes for itself: "the row with this id, if it is
     * failed" means the same thing in every database, so there is nothing driver-specific to
     * get wrong and nothing gained by faking it more loosely. What imitating it buys is the
     * two assertions a recorded result cannot make -- that a second press of Retry is a 409
     * rather than a second 200, and that the memo the route answers with is `queued`, which is
     * what restarts the frontend's poll.
     *
     * What it deliberately does not imitate is the bookkeeping the real statement also writes:
     * `attempts = 0`, `next_attempt_at = now()` and `locked_at = NULL` are not on Memo and
     * never reach a response, so there is nothing here to assert them against. They are the
     * half of this write that only a live Postgres can show, which is MEMO-25's suite -- and
     * MemoRepository::requeue has the argument for why each of them is load-bearing.
     */
    public function requeue(string $memoId): ?Memo
    {
        foreach ($this->rows as $at => $memo) {
            if ($memo->id !== $memoId) {
                continue;
            }

            if ($memo->status !== 'failed') {
                return null;
            }

            // A new Memo rather than a mutated one: Memo's properties are readonly, which is
            // the same reason the real statement answers with RETURNING instead of the row it
            // was handed. `lastError` is carried over unchanged, matching the SQL -- see the
            // repository for why the column is left where it is.
            $queued = new Memo(
                id: $memo->id,
                source: $memo->source,
                status: Memo::STATUS_QUEUED,
                transcript: $memo->transcript,
                title: $memo->title,
                summary: $memo->summary,
                tags: $memo->tags,
                durationMs: $memo->durationMs,
                lastError: $memo->lastError,
                lastErrorCode: $memo->lastErrorCode,
                language: $memo->language,
                createdAt: $memo->createdAt,
                collectionId: $memo->collectionId,
                reminders: $memo->reminders,
            );

            $this->rows[$at] = $queued;

            return $queued;
        }

        return null;
    }

    /**
     * Mirrors the real statement's three conditions, because they are what the endpoint's
     * two 409 messages are about and a fake that accepted anything would let both pass
     * untested. See MemoRepository::retranscribe.
     */
    public function retranscribe(string $memoId, ?string $language): ?Memo
    {
        foreach ($this->rows as $at => $memo) {
            if ($memo->id !== $memoId) {
                continue;
            }

            if ($memo->source !== Memo::SOURCE_VOICE) {
                return null;
            }

            if (! in_array($memo->status, ['ready', 'failed'], true)) {
                return null;
            }

            // `transcript: null` is the part worth copying rather than glossing: the
            // worker decides whether a claimed memo owes a transcript by asking whether it
            // already has one, so a fake that kept the old text would hide the bug where
            // the real UPDATE forgets to clear it.
            //
            // `title`, `summary` and `tags` go the same way, and that is a bug this fake
            // once hid: the title is cut out of the transcript, so a Romanian memo
            // re-decoded from Cyrillic kept the title `Салют`.
            $queued = new Memo(
                id: $memo->id,
                source: $memo->source,
                status: Memo::STATUS_QUEUED,
                transcript: null,
                title: null,
                summary: null,
                tags: [],
                durationMs: $memo->durationMs,
                lastError: null,
                lastErrorCode: null,
                language: $language,
                createdAt: $memo->createdAt,
                collectionId: $memo->collectionId,
                reminders: $memo->reminders,
            );

            $this->rows[$at] = $queued;

            return $queued;
        }

        return null;
    }

    /**
     * Removes the row from `$rows` as well as recording the call.
     *
     * The removal matters for one assertion the recording cannot make: that a second DELETE
     * of the same memo is a 404. Answering from a stored result alone would let the fake
     * report success twice for a row that can only be deleted once, which is the property
     * the route's non-idempotent status code depends on.
     */
    public function delete(string $memoId): ?DeletedMemo
    {
        $this->deleted[] = $memoId;

        foreach ($this->rows as $at => $memo) {
            if ($memo->id === $memoId) {
                unset($this->rows[$at]);
                $this->rows = array_values($this->rows);

                return new DeletedMemo($memo, $this->audioPaths[$memoId] ?? null);
            }
        }

        return null;
    }
}

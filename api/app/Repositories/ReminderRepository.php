<?php

declare(strict_types=1);

namespace App\Repositories;

use App\Services\Owners\OwnerContext;
use Illuminate\Database\DatabaseManager;
use Illuminate\Database\QueryException;
use stdClass;

/**
 * SQL for the reminders table.
 *
 * **The three write methods return a memo id rather than a reminder,** which is the
 * structural difference from the other two repositories. Every route that *changes* a
 * reminder answers with the memo it belongs to, because the frontend reconciles its lists by
 * memo id and the memo already carries its reminders (MemoRepository::COLUMNS aggregates
 * them). So those methods hand back the id the service needs in order to re-read the memo,
 * and the nested reminder's shape stays defined once, in that aggregate, rather than twice.
 *
 * **pending() is the exception, and it is the one place a reminder is a response.** It exists
 * because the browser has to know about reminders on memos that are *not on screen*. The
 * strip only holds unfiled memos, so without this a reminder set on a memo that was then
 * filed into a collection would never fire -- which is exactly the memo somebody cared enough
 * about to organise. It answers a small, flat row per reminder rather than a memo, because
 * this is the one caller that wants many memos' reminders and none of their transcripts.
 *
 * Not final, for the reason the other two are not: the feature suite substitutes a fake,
 * because `php artisan test` runs against sqlite (phpunit.xml) and `coalesce(delivered_at,
 * now())` inside an `UPDATE ... RETURNING` is not something sqlite will answer the same way.
 */
class ReminderRepository
{
    /**
     * SQLSTATE for foreign_key_violation -- here, a reminder for a memo that does not exist.
     *
     * The same code MemoRepository catches for a missing collection, and turned into the same
     * 404 for the same reason: naming something that is not there is the client's mistake and
     * not the server's fault.
     */
    private const FOREIGN_KEY_VIOLATION = '23503';

    public function __construct(
        private readonly DatabaseManager $db,
        private readonly OwnerContext $owner,
    ) {}

    /**
     * The owner every statement in this class is scoped by. See MemoRepository::ownerId.
     *
     * The predicate is on `r.owner_id` rather than on the joined `m.owner_id`, and that is the
     * whole reason 007_owners.sql denormalised the column onto this table: the reminders the
     * browser polls for are found through reminders_owner_due_idx as a range scan over one
     * owner's undelivered rows, instead of walking every owner's in remind_at order and
     * discarding them after the join. The join is still there, but only to label the memo.
     */
    private function ownerId(): string
    {
        return $this->owner->current()->id;
    }

    /**
     * Every reminder still owed, soonest first, with just enough of its memo to name it.
     *
     * **Why this is not "every reminder due now".** The browser needs the ones that have not
     * fired *yet* as well, because that is what it schedules a timer against -- a reminder
     * due in ten minutes has to be known about now if it is to fire while the tab is open.
     * Filtering to `remind_at <= now()` here would leave the client polling to discover the
     * future, which is the thing a scheduled timer exists to avoid.
     *
     * `delivered_at IS NULL` is the whole filter, and it is served by reminders_due_idx --
     * a partial index over exactly these rows, which is why the set that grows without bound
     * (the delivered ones) costs nothing to skip.
     *
     * The label is the same coalesce CollectionRepository uses for its card labels, and for
     * the same reason: a notification saying "Memo" tells the reader nothing, and `title` is
     * null until the enrichment pass runs. Truncated in SQL at 80 characters because a
     * notification body cannot show more and a transcript can be thousands -- this is the one
     * query that would otherwise carry a full transcript per reminder for no reader.
     *
     * Bounded by a caller-supplied limit for the same reason every other list here is: this
     * is an unauthenticated GET and the set is unbounded in principle. A user with more than
     * a screenful of pending reminders has a different problem.
     *
     * @return list<stdClass> Raw rows: `id`, `memo_id`, `memo_label`, `remind_at_iso`, `note`.
     *                        No value object, because there is exactly one consumer and its
     *                        shape is this query -- a Reminder here would have to grow a
     *                        memo label that the nested one does not have, which is two
     *                        classes' worth of divergence for one route.
     */
    public function pending(int $limit): array
    {
        return $this->db->connection()->select(
            <<<'SQL'
                SELECT
                    r.id,
                    r.memo_id,
                    coalesce(m.title, m.summary, left(m.transcript, 80), 'Untitled memo') AS memo_label,
                    to_char(r.remind_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS remind_at_iso,
                    r.note
                FROM reminders r
                JOIN memos m ON m.id = r.memo_id
                WHERE r.owner_id = ?
                  AND r.delivered_at IS NULL
                ORDER BY r.remind_at
                LIMIT ?
                SQL,
            [$this->ownerId(), $limit],
        );
    }

    /**
     * Add a reminder to a memo.
     *
     * @param  string  $remindAt  An absolute instant, already normalised to UTC by
     *                            StoreReminderRequest. A relative "in 30 minutes" was resolved
     *                            against the browser's clock before it was sent, which is why
     *                            nothing here has to know it was ever relative.
     * @return bool Whether the memo exists. False is the controller's 404 -- caught from the
     *              foreign key rather than checked with a SELECT first, because a SELECT would
     *              only race the INSERT and land back here anyway.
     */
    public function insert(string $id, string $memoId, string $remindAt, ?string $note): bool
    {
        // **INSERT ... SELECT rather than VALUES, and the SELECT is the ownership check.**
        // `owner_id` is denormalised onto this table (007_owners.sql has why -- the due-poll
        // index needs it), which raises the obvious question of how the copy is kept honest.
        // The answer is that it is never supplied by a caller: it is read from the memo row in
        // the same statement that files the reminder against it, so the two cannot disagree.
        //
        // The `AND m.owner_id = ?` in that SELECT is what stops one owner setting a reminder
        // on another's memo. A foreign memo selects no row, so the INSERT writes nothing, and
        // `insert()` returning 0 affected rows takes exactly the same path as a memo that does
        // not exist -- which is already this method's 404. No new error case, and the check
        // cannot be skipped by a caller who forgets, because there is no parameter for it.
        //
        // The foreign-key catch below is kept even though the SELECT now makes 23503 much
        // harder to reach: a memo deleted between the SELECT and the write still races, and
        // the catch is what turns that into the same 404 rather than a 500.
        try {
            $affected = $this->db->connection()->affectingStatement(
                <<<'SQL'
                    INSERT INTO reminders (id, memo_id, owner_id, remind_at, note)
                    SELECT ?, m.id, m.owner_id, ?, ?
                      FROM memos m
                     WHERE m.id = ?
                       AND m.owner_id = ?
                    SQL,
                [$id, $remindAt, $note, $memoId, $this->ownerId()],
            );
        } catch (QueryException $e) {
            if (! MemoRepository::isSqlState($e, self::FOREIGN_KEY_VIOLATION)) {
                throw $e;
            }

            return false;
        }

        return $affected > 0;
    }

    /**
     * Record that a reminder has been shown, and say which memo it belongs to.
     *
     * **Idempotent, and that is the point of the coalesce.** `SET delivered_at = now()` would
     * move the timestamp on every acknowledgement, so a second one -- two tabs open, or a
     * retry after a failed response -- would rewrite when the reminder fired. `coalesce`
     * keeps the first delivery, which is the one that answers the only question this column
     * exists for: was this shown, and was it shown on time.
     *
     * The alternative, `WHERE id = ? AND delivered_at IS NULL`, is worse in a way that shows:
     * a second acknowledgement would match no row and come back as a 404, telling the client
     * that a reminder it is looking at does not exist. Idempotent here means a repeat is a
     * successful no-op.
     *
     * @return ?string The memo id, so the caller can re-read the memo and answer with it. Null
     *                 when there is no such reminder.
     */
    public function markDelivered(string $id): ?string
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            'UPDATE reminders SET delivered_at = coalesce(delivered_at, now())'
                .' WHERE id = ? AND owner_id = ? RETURNING memo_id',
            [$id, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row === null ? null : (string) $row->memo_id;
    }

    /**
     * Delete a reminder, and say which memo it belonged to.
     *
     * RETURNING rather than a SELECT beforehand, because the memo id is needed *after* the row
     * is gone -- to answer with the memo in its new state -- and reading it first would be a
     * second round trip for a value the DELETE already has.
     *
     * @return ?string The memo id, or null when there was no such reminder (a 404). A DELETE
     *                 matching nothing is not an error in SQL, so the returned row is the only
     *                 way to tell.
     */
    public function delete(string $id): ?string
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            'DELETE FROM reminders WHERE id = ? AND owner_id = ? RETURNING memo_id',
            [$id, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row === null ? null : (string) $row->memo_id;
    }
}

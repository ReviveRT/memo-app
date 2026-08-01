<?php

declare(strict_types=1);

namespace App\Services\Memos;

use App\Repositories\MemoRepository;
use App\Repositories\ReminderRepository;
use Illuminate\Support\Str;

/**
 * What a reminder is, and what happens to the memo it is attached to.
 *
 * In App\Services\Memos rather than a namespace of its own, because a reminder has no life
 * apart from a memo: it is created under one, cascades when one is deleted
 * (003_collections_and_reminders.sql), and is only ever *read* as part of one. A
 * App\Services\Reminders would be a namespace holding one class that cannot be used without
 * importing this one.
 *
 * **Every method answers with the whole memo, not with the reminder.** That is the decision
 * this class exists to hold, and it is worth stating because it costs a second query each
 * time. The frontend keeps one list of memos and reconciles it by id (MEMO-18): handed a bare
 * reminder it would have to find the memo, splice the reminder into its array, and get the
 * soonest-first ordering right on the client -- three chances to disagree with what the next
 * poll will say. Handed the memo, it writes the row it was already going to write.
 *
 * The two statements are deliberately not wrapped in a transaction. The write is one
 * statement and is already atomic; the read afterwards is allowed to see a memo the worker
 * has since updated, and that is not a problem to solve -- it is a *fresher* answer than the
 * caller asked for, and the next poll would have brought it anyway.
 */
final class ReminderService
{
    /**
     * How many pending reminders the notifier is told about at once.
     *
     * Not configurable and not a query parameter, because there is one caller and it wants
     * all of them -- the number is a safety bound rather than a page size. A hundred pending
     * reminders is already far past the point where notifications are useful, so a user who
     * reaches it will not be helped by the hundred-and-first.
     */
    public const PENDING_LIMIT = 100;

    public function __construct(
        private readonly ReminderRepository $reminders,
        private readonly MemoRepository $memos,
    ) {}

    /**
     * Every reminder still owed, soonest first, each with a label naming its memo.
     *
     * The one read here that is not about a single memo, and the only reason it exists is
     * that the browser cannot see the whole database. The fast strip holds unfiled memos and
     * an opened collection holds one collection's; a reminder set on a memo and then filed
     * away belongs to neither, and would silently never fire. This is what the delivery loop
     * polls instead of trying to assemble the answer from whatever lists happen to be
     * mounted.
     *
     * Rows rather than value objects, deliberately -- see the repository. There is one
     * consumer and its shape is this query.
     *
     * @return list<array<string, mixed>>
     */
    public function pending(): array
    {
        return array_map(
            static fn (object $row): array => [
                'id' => (string) $row->id,
                'memo_id' => (string) $row->memo_id,
                'memo_label' => (string) $row->memo_label,
                'remind_at' => (string) $row->remind_at_iso,
                'note' => $row->note === null ? null : (string) $row->note,
            ],
            $this->reminders->pending(self::PENDING_LIMIT),
        );
    }

    /**
     * Set a reminder on a memo, and hand back the memo carrying it.
     *
     * The id is a UUIDv7 minted here for the reasons MemoService::createFromText gives about a
     * memo's: it is time-ordered, so rows land at the right-hand edge of the primary key
     * index, and the caller does not have to ask the database what it just wrote.
     *
     * Nothing here refuses a second reminder on the same memo, or two reminders at the same
     * instant. Both are things a user can reasonably want -- an alarm for the morning and a
     * timer for the next twenty minutes are the two controls the UI offers, and they are not
     * alternatives -- which is the whole reason reminders are a table rather than a pair of
     * columns on the memo.
     *
     * @param  string  $remindAt  An absolute instant in UTC.
     * @return ?Memo Null when there is no such memo, which the controller answers as a 404.
     */
    public function add(string $memoId, string $remindAt, ?string $note): ?Memo
    {
        if (! $this->reminders->insert(Str::uuid7()->toString(), $memoId, $remindAt, $note)) {
            return null;
        }

        // Re-read rather than assembling the answer from what was just written. The memo may
        // have gained a transcript or a title since it was last sent to this client, and the
        // reminders aggregate has to come back in soonest-first order across *all* of them,
        // which only the query knows.
        return $this->memos->find($memoId);
    }

    /**
     * Record that a reminder has been shown, and hand back its memo.
     *
     * This is what stops a reminder firing on every page load. The browser fires it, then
     * calls this; until it does, the reminder is still owed, which is the correct state to be
     * in if the tab was closed between the two.
     *
     * Idempotent -- see ReminderRepository::markDelivered for why the first delivery time is
     * the one kept.
     *
     * @return ?Memo Null when there is no such reminder.
     */
    public function markDelivered(string $reminderId): ?Memo
    {
        $memoId = $this->reminders->markDelivered($reminderId);

        return $memoId === null ? null : $this->memos->find($memoId);
    }

    /**
     * Remove a reminder, and hand back the memo without it.
     *
     * A separate operation from acknowledging it, and both are needed: acknowledging says
     * "I have seen this", deleting says "I did not want it". A UI that only had the first
     * would make a mis-set alarm unfixable except by waiting for it to go off.
     *
     * @return ?Memo Null when there is no such reminder.
     */
    public function remove(string $reminderId): ?Memo
    {
        $memoId = $this->reminders->delete($reminderId);

        return $memoId === null ? null : $this->memos->find($memoId);
    }
}

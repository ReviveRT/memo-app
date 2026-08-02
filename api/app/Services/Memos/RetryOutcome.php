<?php

declare(strict_types=1);

namespace App\Services\Memos;

/**
 * What happened when somebody pressed Retry: the memo went back to the queue, or it did not,
 * and if it did not, whether there was a memo there at all.
 *
 * **Three outcomes rather than the two a `?Memo` can carry, and the third one is the reason
 * this class exists.** Every other write on this resource collapses its failures into one
 * 404 -- `moveToCollection` answers null for a missing memo *and* for a missing collection,
 * because from the client's side both are "you named something that is not there" and the
 * remedy is the same. Retry does not fit that test. A memo that is gone and a memo that has
 * since transcribed are different facts, and the API's message is rendered to the user
 * verbatim: telling somebody that a memo they are looking at no longer exists, when what
 * actually happened is that it succeeded while they were reaching for the button, is a worse
 * answer than no message.
 *
 * The refused case carries the memo rather than just its status, so the response can say what
 * state it is actually in and the caller has the row in hand -- which is what a client
 * holding a stale `failed` card needs to correct itself.
 *
 * Named constructors and a private one, so the impossible combinations cannot be built. There
 * is no outcome with `requeued = true` and no memo, and nothing has to test for one.
 */
final class RetryOutcome
{
    private function __construct(
        /** The memo as it now is, or null when there is no such memo. */
        public readonly ?Memo $memo,

        /** Whether this call is what put it back in the queue. */
        public readonly bool $requeued,
    ) {}

    /** It was failed, and it is queued again. */
    public static function requeued(Memo $memo): self
    {
        return new self($memo, true);
    }

    /**
     * The memo exists but was not failed, so nothing was done to it.
     *
     * Reachable by an ordinary double click and by a second tab, not only by a malformed
     * client: the worker may finish a memo between the list being rendered and the button
     * being pressed. MemoRepository::requeue has why requeueing a `processing` or `ready` row
     * is refused rather than tolerated.
     */
    public static function refused(Memo $memo): self
    {
        return new self($memo, false);
    }

    /** No row with that id. */
    public static function missing(): self
    {
        return new self(null, false);
    }
}

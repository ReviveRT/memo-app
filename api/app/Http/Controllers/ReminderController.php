<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Http\Requests\StoreReminderRequest;
use App\Services\Memos\ReminderService;
use Illuminate\Http\JsonResponse;
use Symfony\Component\HttpFoundation\Response;

/**
 * The three things that happen to a reminder: it is set, it is shown, and it is removed.
 *
 * **Every one of the three writes answers `{"memo": {...}}`.** Not `{"reminder": ...}`, and
 * that is the decision worth reading before changing any of them. The frontend holds its
 * memos keyed by id and reconciles them against whatever the API last said (MEMO-18); a
 * response carrying the memo is a row it can write directly. A response carrying the reminder
 * alone would make it find the memo, splice the reminder into its array and reproduce the
 * soonest-first ordering client-side -- three places to drift from what the next poll returns.
 *
 * The cost is one extra SELECT per call, which ReminderService pays deliberately.
 *
 * index() is the exception and answers reminders, because it is a read across every memo by a
 * caller holding none of them. Its own docblock has the argument.
 *
 * Two of these routes are scoped under the memo and one is not, which is a difference the URLs
 * are telling the truth about: creating a reminder needs to say which memo it is for, while
 * acknowledging or deleting an existing one identifies it by its own id and the memo is
 * already implied. A `/memos/{memo}/reminders/{reminder}` for those two would put a value in
 * the path that nothing reads, and that nothing checks agrees with the reminder.
 */
final class ReminderController extends Controller
{
    public function __construct(private readonly ReminderService $reminders) {}

    /**
     * Every reminder still owed, for the browser's delivery loop.
     *
     * The one route here that answers with reminders rather than with a memo, and the
     * exception is load-bearing rather than an inconsistency. The rule above is about
     * *writes*: those change one memo and the client is holding that memo. This is a read
     * across all of them, by a caller that is not looking at any -- the loop has to know
     * about a reminder on a memo filed inside a collection nobody has opened, and there is no
     * memo in the client's hands to attach it to.
     *
     * Each row carries `memo_label` so a notification can name what it is about, and
     * `memo_id` so pressing it can open the right card. It deliberately does not carry the
     * memo: the transcript is the largest thing on the row and a notification body shows
     * eighty characters.
     *
     * No parameters. "Still owed" is the whole question, and narrowing it to "due now" would
     * defeat the point -- the loop schedules a timer against reminders that have *not* fired
     * yet, which it can only do if it is told about them in advance.
     *
     * `no-store` for the reason the memo list has it: this is polled, and a cached answer is
     * a reminder that fires late or twice.
     */
    public function index(): JsonResponse
    {
        return response()
            ->json(['reminders' => $this->reminders->pending()])
            ->header('Cache-Control', 'no-store');
    }

    /**
     * Set a reminder on a memo.
     *
     * 201, because a reminder was created, even though the body is the memo. The status
     * describes what happened; the body is what the client needs in order to render it.
     */
    public function store(StoreReminderRequest $request, string $memo): JsonResponse
    {
        $updated = $this->reminders->add($memo, $request->remindAt(), $request->note());

        if ($updated === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo no longer exists. Refresh and try again.');
        }

        return response()->json(['memo' => $updated->toArray()], Response::HTTP_CREATED);
    }

    /**
     * Record that a reminder has been shown.
     *
     * Called by the browser immediately after it puts a notification on screen, and it is what
     * makes a reminder fire once rather than once per page load. Until this lands the reminder
     * is still owed -- which is the right state if the tab was closed in between, because
     * nothing has actually told the user anything yet.
     *
     * PATCH with no body rather than POST to a `/delivered` sub-path. It is an idempotent
     * change to one field of an existing resource, which is what PATCH is, and
     * ReminderService::markDelivered is genuinely idempotent -- the first delivery timestamp
     * wins, so a retry after a lost response is safe and answers the same thing.
     *
     * No FormRequest, because there is nothing to validate: what the field becomes is decided
     * here (`now()`, in SQL) rather than sent by the client. Letting the client name the
     * delivery time would mean trusting a browser's clock to write a column used to judge
     * whether reminders arrive late.
     */
    public function update(string $reminder): JsonResponse
    {
        $updated = $this->reminders->markDelivered($reminder);

        if ($updated === null) {
            abort(Response::HTTP_NOT_FOUND, 'That reminder no longer exists.');
        }

        return response()->json(['memo' => $updated->toArray()]);
    }

    /**
     * Remove a reminder.
     *
     * 200 with the memo rather than 204, unlike deleting a collection. The difference is that
     * a memo still exists here and its `reminders` array has changed, so there is something
     * the client needs; a deleted collection leaves nothing to describe.
     */
    public function destroy(string $reminder): JsonResponse
    {
        $updated = $this->reminders->remove($reminder);

        if ($updated === null) {
            abort(Response::HTTP_NOT_FOUND, 'That reminder no longer exists.');
        }

        return response()->json(['memo' => $updated->toArray()]);
    }
}

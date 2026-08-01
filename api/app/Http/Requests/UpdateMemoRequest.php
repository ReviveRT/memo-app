<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for PATCH /api/memos/{memo}.
 *
 * One field, and the route exists for one reason: filing a memo into a collection, or
 * taking it back out. That is the whole of what "move a fast memo into the collection you
 * want" needs, and it is deliberately not a general-purpose memo editor -- the transcript
 * is the worker's output and the status is the queue's, so neither is something a client
 * gets to set.
 *
 * PATCH rather than PUT, and that is not pedantry here: PUT means "replace the resource
 * with this body", and a body carrying only `collection_id` would be asking the API to
 * discard the transcript. PATCH means "apply this change", which is what it is.
 */
final class UpdateMemoRequest extends FormRequest
{
    /**
     * `present` plus `nullable`, which is the pair that makes the two states of this field
     * distinguishable -- and both states are real operations:
     *
     *   * `{"collection_id": "<uuid>"}` files the memo there.
     *   * `{"collection_id": null}` returns it to the fast strip. This is how a memo filed
     *     by mistake gets back out, so null has to be a value the API accepts rather than
     *     a way of saying nothing.
     *   * `{}` is neither, and `present` is what turns it into a 422 that names the field
     *     instead of a 200 that changed nothing. A PATCH that silently no-ops is the worst
     *     of the three outcomes: the client believes the move happened.
     *
     * `nullable` alone would not do it -- a missing key satisfies `nullable` -- and
     * `required` would reject the null that unfiles. The two rules together are the only
     * combination that means "say something, and null counts as saying something".
     *
     * ConvertEmptyStringsToNull is in the global middleware stack, so `""` arrives here as
     * null and unfiles the memo. That is the reading this route wants, and it is the same
     * one ListMemosRequest applies to a blank `q`.
     *
     * No `exists:collections,id`. It would be a second query and a race -- the collection
     * can be deleted between the check and the UPDATE -- so the foreign key is the check,
     * and MemoRepository::moveToCollection turns its violation into the same 404 a missing
     * memo gets. The `uuid` rule still earns its place: it rejects a malformed id before a
     * statement is sent, and without it Postgres raises a type error that surfaces as a
     * 500.
     *
     * @return array<string, list<string>>
     */
    public function rules(): array
    {
        return [
            'collection_id' => ['present', 'nullable', 'uuid'],
        ];
    }

    /**
     * Renamed for the message, for the reason ListMemosRequest renames `q`: the frontend
     * renders a failed request's `message` verbatim, and "The collection_id field must be
     * present" names a JSON key rather than the thing the user was trying to do.
     *
     * @return array<string, string>
     */
    public function attributes(): array
    {
        return ['collection_id' => 'collection'];
    }

    /**
     * The collection to file into, or null to unfile.
     *
     * The distinction this method does *not* have to preserve is absent-versus-null: by the
     * time it runs, `present` has rejected absent, so null unambiguously means unfile.
     */
    public function collectionId(): ?string
    {
        $id = $this->validated()['collection_id'] ?? null;

        return is_string($id) && $id !== '' ? $id : null;
    }
}

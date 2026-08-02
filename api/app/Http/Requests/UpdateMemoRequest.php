<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\NoNullBytes;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Validator;

/**
 * Validation for PATCH /api/memos/{memo}.
 *
 * Two fields, either of them on its own, and that is as far as this route goes. It is
 * deliberately not a general-purpose memo editor:
 *
 *   * `collection_id` files a memo into a collection, or takes it back out.
 *   * `title` renames it.
 *
 * **Why `title` is writable and `transcript` is not.** The transcript is a record of what
 * was said. memo_ai/prose.py will not so much as respell a word of it -- there is an
 * invariant and a test asserting that the formatter touches only whitespace, punctuation and
 * case -- precisely so that the column can be trusted as evidence of the recording. A client
 * that could edit it would remove the one property that makes it worth keeping.
 *
 * A title is the opposite kind of thing. It is *generated*: cut from the transcript as a
 * fallback, then replaced by whatever the enrichment pass makes of it. That is a guess about
 * what a memo should be called, and a wrong guess on a strip of thirty cards is a memo the
 * owner cannot find again. So the guess is the default and the owner has the last word.
 *
 * `status`, `tags` and the rest stay out for the first reason rather than the second: they
 * are the queue's and the worker's, and a client setting `status` would be a client claiming
 * a job.
 *
 * PATCH rather than PUT, and that is not pedantry here: PUT means "replace the resource with
 * this body", and a body carrying only `collection_id` would be asking the API to discard the
 * transcript. PATCH means "apply this change", which is what it is.
 */
final class UpdateMemoRequest extends FormRequest
{
    /**
     * Mirrors the cap the column is given elsewhere in this app: 200 characters is longer
     * than any title the enrichment pass produces and short enough that the field cannot be
     * used as a second transcript. Repeated here rather than derived from the schema, the
     * same way StoreMemoRequest repeats its own -- the database has no length constraint on
     * `title`, so this *is* the constraint.
     */
    public const MAX_TITLE_LENGTH = 200;

    /**
     * **`sometimes` on both, with a rule below that at least one arrived.**
     *
     * This used to be a single `collection_id` under `present` + `nullable`, and the
     * reasoning for that pair is worth keeping because half of it still applies. Both states
     * of the field are real operations -- an id files the memo, an explicit null returns it
     * to the fast strip -- so `nullable` alone would not do (a missing key satisfies it) and
     * `required` would reject the null that unfiles. `present` was what made `{}` a 422
     * naming the field rather than a 200 that changed nothing, which is the worst of the
     * three outcomes: the client believes the move happened.
     *
     * With a second field, `present` on the first is simply wrong -- a rename would have to
     * send `collection_id` to say "leave it where it is", which is a client asserting a value
     * it did not mean to write and cannot know is still current. So each field is
     * `sometimes`, and the "say something" guarantee moves to withValidator() below, where it
     * can be stated about the body as a whole.
     *
     * ConvertEmptyStringsToNull is in the global middleware stack, so `""` arrives here as
     * null for both fields: an empty collection unfiles the memo, and an empty title clears
     * it. Both are the readings this route wants, and both are the same reading
     * ListMemosRequest applies to a blank `q`.
     *
     * No `exists:collections,id`. It would be a second query and a race -- the collection can
     * be deleted between the check and the UPDATE -- so the foreign key is the check, and
     * MemoRepository::moveToCollection turns its violation into the same 404 a missing memo
     * gets. The `uuid` rule still earns its place: it rejects a malformed id before a
     * statement is sent, and without it Postgres raises a type error that surfaces as a 500.
     *
     * NoNullBytes on the title for the reason StoreMemoRequest applies it to text: Postgres
     * refuses `\0` in a `text` column with a 22021 that reads as a server fault, so it is
     * caught here where it can be answered as the bad request it is.
     *
     * @return array<string, list<mixed>>
     */
    public function rules(): array
    {
        return [
            'collection_id' => ['sometimes', 'nullable', 'uuid'],
            'title' => ['sometimes', 'nullable', 'string', new NoNullBytes, 'max:'.self::MAX_TITLE_LENGTH],
        ];
    }

    /**
     * A body that changes nothing is a 422, not a 200.
     *
     * The property `present` used to give one field, restated for two: a PATCH that silently
     * no-ops is indistinguishable from one that worked, so the client goes on believing the
     * rename or the move happened. Checked against the raw input rather than the validated
     * set, because a field explicitly sent as null is *present* and means something, and
     * `validated()` cannot tell that from absent.
     */
    public function withValidator(Validator $validator): void
    {
        $validator->after(function (Validator $validator): void {
            if (! $this->has('collection_id') && ! $this->has('title')) {
                $validator->errors()->add(
                    'title',
                    'Send a collection or a title — this request asks for no change.',
                );
            }
        });
    }

    /**
     * Renamed for the message, for the reason ListMemosRequest renames `q`: the frontend
     * renders a failed request's `message` verbatim, and "The collection_id field must be a
     * valid UUID" names a JSON key rather than the thing the user was trying to do.
     *
     * @return array<string, string>
     */
    public function attributes(): array
    {
        return ['collection_id' => 'collection', 'title' => 'title'];
    }

    /** Whether the body asked for the memo to be filed or unfiled. */
    public function movesCollection(): bool
    {
        return $this->has('collection_id');
    }

    /**
     * The collection to file into, or null to unfile.
     *
     * Only meaningful when movesCollection() is true. Absent and explicitly-null both read as
     * null here, which is why the caller has to ask that question first rather than inferring
     * it from this.
     */
    public function collectionId(): ?string
    {
        $id = $this->validated()['collection_id'] ?? null;

        return is_string($id) && $id !== '' ? $id : null;
    }

    /** Whether the body asked for a rename. */
    public function renames(): bool
    {
        return $this->has('title');
    }

    /**
     * The new title, or null to clear it and fall back to the transcript.
     *
     * Trimmed, and whitespace-only becomes null, so the API stores one spelling of "no title
     * of its own". MemoService trims again on the way to the UPDATE -- agreement rather than
     * reliance, and that side is the one the database is on.
     */
    public function title(): ?string
    {
        $title = $this->validated()['title'] ?? null;

        if (! is_string($title)) {
            return null;
        }

        $trimmed = trim($title);

        return $trimmed === '' ? null : $trimmed;
    }
}

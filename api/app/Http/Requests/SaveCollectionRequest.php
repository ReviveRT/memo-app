<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\NoNullBytes;
use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for POST /api/collections and PATCH /api/collections/{collection}.
 *
 * One class for both, which is a departure from the usual Store/Update pair and is worth
 * saying why: the two requests validate the same single field under the same rules, because
 * naming a collection and renaming it are the same act. A second class would be a copy that
 * exists to be kept in step, and the way that copy goes wrong is asymmetrically -- a cap
 * raised on create and not on rename produces a collection that cannot be renamed to its own
 * name.
 *
 * If the two ever diverge -- a description on create, say -- this splits. Nothing about
 * sharing it now makes that harder.
 */
final class SaveCollectionRequest extends FormRequest
{
    /**
     * Long enough for a real name, short enough to render on a card.
     *
     * "Memos for Work" is 14 characters; 120 leaves room for something far more specific
     * without the grid having to cope with a paragraph. The card truncates in CSS as well,
     * because a cap is not a layout guarantee -- 120 characters of "WWWW" is still wider than
     * a card -- but the cap is what keeps the *stored* value sane.
     *
     * Not shared with StoreMemoRequest::MAX_TEXT_LENGTH or ListMemosRequest's query cap, for
     * the reason those two are not shared with each other: a memo, a search for one and the
     * name of a folder are three different sizes of thing, and one constant serving all of
     * them would be a number none of them chose.
     */
    public const MAX_NAME_LENGTH = 120;

    /**
     * Trimmed before validation, so the length rule and the uniqueness index judge the same
     * string the database will hold.
     *
     * This is what makes `"  "` a 422 rather than a 500: untrimmed it satisfies `required` and
     * `min:1`, reaches Postgres, and is refused there by the CHECK constraint on
     * `btrim(name) <> ''` -- which arrives as an unhandled QueryException. Trimming here turns
     * it into "The name field is required." next to the box.
     *
     * It also makes the unique index mean what a user expects. That index is over
     * `lower(btrim(name))`, so " Work " and "work" already collide in the database; trimming
     * on the way in is what stops a collection being *stored* with padding it will then never
     * match its own name by.
     */
    protected function prepareForValidation(): void
    {
        $name = $this->input('name');

        if (is_string($name)) {
            $this->merge(['name' => trim($name)]);
        }
    }

    /**
     * `required`, not `present` + `nullable` -- the opposite of UpdateMemoRequest, and for a
     * reason rather than by inconsistency. There is no such thing as a collection with no
     * name: null would be a card nobody can find or identify. A memo's `collection_id`, by
     * contrast, has a real null meaning (unfiled), which is why that route has to accept one.
     *
     * NoNullBytes for the reason the rule class itself gives: libpq truncates a bound
     * parameter at the first NUL, so without this a collection named "Wo\0rk" is stored as
     * "Wo" and the 201 reports a name the user did not choose. The trim above already removes
     * a NUL at either edge -- PHP's default charlist includes it -- so this is the interior
     * case, which nothing else catches.
     *
     * No `unique:collections,name` rule. It would be a second query, it would be racy, and it
     * would be *wrong*: the index is over `lower(btrim(name))`, so a Laravel `unique` rule
     * comparing raw values would pass "work" against a stored "Work" and then hit a 500 on
     * the insert. The index is the check, and CollectionRepository turns its violation into
     * this field's 422.
     *
     * @return array<string, list<string|NoNullBytes>>
     */
    public function rules(): array
    {
        return [
            'name' => ['required', 'string', 'min:1', 'max:'.self::MAX_NAME_LENGTH, new NoNullBytes],
        ];
    }

    /** The validated name, trimmed and non-empty. */
    public function name(): string
    {
        return (string) $this->validated()['name'];
    }
}

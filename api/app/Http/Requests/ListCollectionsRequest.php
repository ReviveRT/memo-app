<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Requests\Concerns\FiltersByTime;
use App\Http\Rules\NoNullBytes;
use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for GET /api/collections.
 *
 * Four parameters, and three of them are deliberately the same three ListMemosRequest takes:
 * `q`, `from` and `to`. The brief asks for one filter that behaves the same over collections
 * as over memos, so the parameters are spelled the same way, capped at the same lengths, and
 * mean the same things -- a shared date picker in the UI sends identical query strings to
 * both routes.
 *
 * What it does not take is `collection`, for the obvious reason, and that is the whole of the
 * difference between the two request classes.
 */
final class ListCollectionsRequest extends FormRequest
{
    use FiltersByTime;

    /**
     * The grid is a single unpaginated page, like the memo list, so this is the only thing
     * bounding its size.
     *
     * Lower than ListMemosRequest's 200, and the reason is what a row costs rather than
     * timidity. Each collection carries a count plus three transcript slices, and the count
     * is a correlated subquery -- so 200 collections is 200 counts and 600 substrings, for a
     * grid whose whole design is three rows deep with the rest behind a scroll. 100 is far
     * past what anyone will make and still cheap.
     */
    public const MAX_LIMIT = 100;

    /** Enough to fill the three-row grid several times over. */
    public const DEFAULT_LIMIT = 50;

    /**
     * Shared with ListMemosRequest's cap on `q` rather than chosen separately, because it is
     * the same box in the interface and the same two Postgres consumers on this side -- one
     * `websearch_to_tsquery` parse and one ILIKE pattern per request, on an unauthenticated
     * GET.
     *
     * Not literally shared as a constant reference. ListMemosRequest's own note explains why
     * these numbers are repeated rather than pointed at each other: a constant serving two
     * routes is a number neither of them chose, and the day one needs to move it would move
     * the other silently.
     */
    public const MAX_QUERY_LENGTH = 200;

    /**
     * Trimmed here as well as by the global TrimStrings middleware, for the reason
     * ListMemosRequest gives about its own: the rule that `?q=%20%20` means "no filter"
     * belongs to this class, and leaving it to the middleware makes it revocable from
     * bootstrap/app.php by somebody with an unrelated reason to change the global stack.
     */
    protected function prepareForValidation(): void
    {
        $query = $this->input('q');

        if (is_string($query)) {
            $this->merge(['q' => trim($query)]);
        }
    }

    /**
     * @return array<string, mixed>
     */
    public function rules(): array
    {
        return $this->timeWindowRules() + [
            'limit' => ['sometimes', 'nullable', 'integer', 'min:1', 'max:'.self::MAX_LIMIT],
            'q' => ['sometimes', 'nullable', 'string', 'max:'.self::MAX_QUERY_LENGTH, new NoNullBytes],
        ];
    }

    /**
     * @return array<string, string>
     */
    public function attributes(): array
    {
        return ['q' => 'filter'] + $this->timeWindowAttributes();
    }

    /**
     * Rejected rather than clamped, for the reason ListMemosRequest gives: a silently clamped
     * `?limit=5000` answers 200 with 100 rows and looks like there are only 100 collections.
     */
    public function limit(): int
    {
        return (int) ($this->validated()['limit'] ?? self::DEFAULT_LIMIT);
    }

    /**
     * The filter, or null when there is none.
     *
     * Named to match ListMemosRequest's accessor, and not `query()`, because
     * Illuminate\Http\Request already has a method by that name for reading query-string
     * input and overriding it on a FormRequest would break every framework caller of
     * `$request->query('...')`.
     *
     * `!== ''` rather than a truthiness test: '0' is falsy in PHP and is a perfectly good
     * thing to call a collection.
     */
    public function searchQuery(): ?string
    {
        $query = $this->validated()['q'] ?? null;

        return is_string($query) && $query !== '' ? $query : null;
    }
}

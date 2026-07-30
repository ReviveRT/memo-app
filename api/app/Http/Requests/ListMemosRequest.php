<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for GET /api/memos.
 *
 * One parameter today. MEMO-19 adds `q` here, and the reason this class exists for
 * a single integer is that the cap below has to be stated somewhere a search filter
 * cannot quietly widen.
 */
final class ListMemosRequest extends FormRequest
{
    /** The default the task specifies. Enough to fill a screen several times over. */
    public const DEFAULT_LIMIT = 50;

    /**
     * The response is a single unpaginated page, so this is the only thing bounding
     * its size -- and the rows carry full transcripts. A cap that can be raised from
     * the query string is not a cap, and there is no pagination to fall back on
     * (MEMO-18 replaces the page by id, MEMO-19 filters it; neither uses a cursor).
     */
    public const MAX_LIMIT = 200;

    /**
     * @return array<string, list<string>>
     */
    public function rules(): array
    {
        return [
            // nullable, so `?limit=` means "unset" rather than 422. That matches
            // App\Support\Env, which exists because a set-but-empty value is the
            // normal output of a template that had nothing to put in it -- here, a
            // frontend building a query string with no limit chosen. Without
            // nullable, ConvertEmptyStringsToNull hands the `integer` rule a null and
            // it fails.
            //
            // `integer` rather than `numeric`: it validates with FILTER_VALIDATE_INT,
            // so "50" from the query string passes and "50.5" does not.
            'limit' => ['sometimes', 'nullable', 'integer', 'min:1', 'max:'.self::MAX_LIMIT],
        ];
    }

    /**
     * Rejected rather than clamped, which is why this only has to apply the default.
     * A silently clamped ?limit=5000 answers 200 with 200 rows and looks to the
     * caller like there are only 200 memos; a 422 naming the cap cannot be
     * misread.
     */
    public function limit(): int
    {
        return (int) ($this->validated()['limit'] ?? self::DEFAULT_LIMIT);
    }
}

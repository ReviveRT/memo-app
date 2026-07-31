<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\NoNullBytes;
use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for GET /api/memos.
 *
 * Two parameters: `limit`, which bounds the single unpaginated page, and `q`, which
 * filters it. Neither may quietly widen the other -- see MAX_LIMIT.
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
     * A filter box, not a document.
     *
     * The cap is not decoration. This string reaches Postgres twice per request -- once
     * as `websearch_to_tsquery` input and once as the body of an ILIKE pattern -- on an
     * unauthenticated GET, so an uncapped `q` is an uncapped parser and an uncapped
     * pattern match. 200 characters is longer than any quoted phrase anyone types and
     * short enough that a scripted flood is a nuisance rather than a load test.
     *
     * Not shared with StoreMemoRequest::MAX_TEXT_LENGTH on purpose. A memo and a query
     * about it are different sizes of thing, and one constant serving both would be a
     * number neither of them chose.
     */
    public const MAX_QUERY_LENGTH = 200;

    /**
     * Trimmed here as well as by the global TrimStrings middleware, for the reason
     * StoreMemoRequest gives about its own trim: the rule that `?q=%20%20` means "no
     * filter" rather than "match everything containing a space" is this class's, and
     * leaving it to the middleware makes it revocable from bootstrap/app.php by someone
     * with an unrelated reason to change the global stack.
     *
     * The trim is what makes the blank case reach searchQuery() as an empty string;
     * ConvertEmptyStringsToNull then turns it into null, and searchQuery() treats both
     * the same way regardless of which middleware is in the stack.
     */
    protected function prepareForValidation(): void
    {
        $query = $this->input('q');

        if (is_string($query)) {
            $this->merge(['q' => trim($query)]);
        }
    }

    /**
     * @return array<string, list<string|NoNullBytes>>
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

            // nullable for the same reason as `limit`, and it carries more weight here:
            // the frontend's search box is empty most of the time, so `?q=` is the
            // ordinary request rather than the edge case. A 422 for it would mean the
            // list could not be loaded until something was typed.
            //
            // No `min:1`. An empty `q` is "no filter", which is a valid request and not
            // a validation failure -- the difference from StoreMemoRequest, where a
            // blank `text` is a memo with nothing in it.
            'q' => ['sometimes', 'nullable', 'string', 'max:'.self::MAX_QUERY_LENGTH, new NoNullBytes],
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

    /**
     * The filter, or null when there is none.
     *
     * Deliberately not called `query()`: Illuminate\Http\Request already has a method by
     * that name for reading query-string input, and overriding it on a FormRequest would
     * break every caller of `$request->query('...')` in the framework.
     *
     * Empty means absent. Both spellings of "the user has not typed anything" -- the
     * parameter missing, and the parameter present but blank -- collapse here rather
     * than at the three call sites downstream, so no layer below this has to decide
     * whether '' is a filter that matches everything or no filter at all.
     */
    public function searchQuery(): ?string
    {
        $query = $this->validated()['q'] ?? null;

        return is_string($query) && $query !== '' ? $query : null;
    }
}

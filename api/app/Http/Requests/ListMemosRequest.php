<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Requests\Concerns\FiltersByTime;
use App\Http\Rules\NoNullBytes;
use App\Services\Memos\MemoQuery;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Support\Str;

/**
 * Validation for GET /api/memos.
 *
 * Five parameters now: `limit`, which bounds the single unpaginated page, `q`, which
 * filters it by text, `from` and `to`, which bound it by creation time, and `collection`,
 * which scopes it to one collection or to the memos in none. None of them may quietly
 * widen another -- see MAX_LIMIT.
 *
 * All four filters are independent and any combination is a valid request, which is why
 * the repository assembles one statement rather than offering a method per shape.
 */
final class ListMemosRequest extends FormRequest
{
    use FiltersByTime;

    /**
     * `?collection=none` -- the fast strip: memos filed nowhere.
     *
     * One parameter with three readings (absent, `none`, an id) rather than a
     * `collection_id` plus an `unassigned` flag, and that is worth the magic value. Two
     * parameters can contradict each other -- `?collection_id=<uuid>&unassigned=1` asks
     * for a memo that is both filed and unfiled -- and something then has to decide which
     * one wins, in a place no reader of the URL can see. One parameter cannot say both.
     *
     * Not a collision risk with a real id: this is matched before the uuid check, and no
     * uuid is the string `none`.
     */
    public const COLLECTION_NONE = 'none';

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
     * @return array<string, mixed>
     */
    public function rules(): array
    {
        return $this->timeWindowRules() + [
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

            // A closure rather than a rule class, unlike NoNullBytes and SniffedAudioType.
            // Those two exist as classes because what they enforce is subtle and shared --
            // the driver's NUL truncation, and sniffing bytes rather than trusting a
            // filename. This is neither: it is one field on one route, and the two things
            // it accepts are both named in the message it fails with.
            //
            // Checked in this order because `none` is not a uuid, so the uuid test has to
            // be the fallback rather than the gate.
            'collection' => [
                'sometimes',
                'nullable',
                'string',
                function (string $attribute, mixed $value, callable $fail): void {
                    if ($value === self::COLLECTION_NONE || Str::isUuid($value)) {
                        return;
                    }

                    $fail(
                        'The :attribute field must be a collection id or "'
                            .self::COLLECTION_NONE.'" for memos in no collection.'
                    );
                },
            ],
        ];
    }

    /**
     * `q` is renamed for the message, because the message is not read by whoever wrote the
     * query string.
     *
     * The frontend renders a failed GET's `message` verbatim -- web/src/api/memos.js is
     * explicit that every error it can produce has to say something a human can act on --
     * and this one is reachable without meaning to: pasting a paragraph into the filter box
     * answers 422, and the default wording is "The q field must not be greater than 200
     * characters." Named, it reads "The filter field ...", which is the box the user is
     * looking at.
     *
     * `limit` is left alone. It is already an English word, and nothing in the UI sends it.
     * `collection` is left alone too, and for a stronger reason: its only failure message
     * is the one written in rules() above, which already names what it wants.
     *
     * The trait's two are merged rather than inherited, because a trait cannot contribute
     * to a method the using class also defines -- PHP resolves that as the class winning
     * outright, silently. Spelling the merge out is what stops `from` and `to` losing their
     * names the moment this method exists.
     *
     * @return array<string, string>
     */
    public function attributes(): array
    {
        return ['q' => 'filter'] + $this->timeWindowAttributes();
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
     *
     * `!== ''` rather than a truthiness test, and it is not style: '0' is falsy in PHP and
     * is a perfectly good thing to search for. `empty($query)` here would make a filter of
     * "0" mean "no filter" and quietly answer the whole list. Pinned by a test for exactly
     * that query.
     */
    public function searchQuery(): ?string
    {
        $query = $this->validated()['q'] ?? null;

        return is_string($query) && $query !== '' ? $query : null;
    }

    /**
     * Everything above, as the one object the service and repository take.
     *
     * Assembled here rather than in the controller because this is the class that knows
     * what each parameter means once validated -- that a blank `q` is no filter, that
     * `collection=none` is not a collection id, that an absent `limit` is
     * DEFAULT_LIMIT. The controller's job is to hand the result on and serialise what
     * comes back.
     *
     * The three-way read of `collection` happens here and nowhere else, which is the
     * point of the parameter being one field: `unfiledOnly` and `collectionId` reach
     * MemoQuery already reconciled, so no layer below this can be handed both.
     */
    public function memoQuery(): MemoQuery
    {
        $collection = $this->validated()['collection'] ?? null;
        $collection = is_string($collection) && $collection !== '' ? $collection : null;

        return new MemoQuery(
            window: $this->timeWindow(),
            text: $this->searchQuery(),
            collectionId: $collection === self::COLLECTION_NONE ? null : $collection,
            unfiledOnly: $collection === self::COLLECTION_NONE,
            limit: $this->limit(),
        );
    }
}

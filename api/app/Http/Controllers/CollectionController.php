<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Http\Requests\ListCollectionsRequest;
use App\Http\Requests\SaveCollectionRequest;
use App\Services\Collections\Collection;
use App\Services\Collections\CollectionService;
use Illuminate\Http\JsonResponse;
use Illuminate\Validation\ValidationException;
use Symfony\Component\HttpFoundation\Response;

/**
 * HTTP in, HTTP out, for the four things a collection can have done to it. No SQL, and no
 * decisions about what a collection is.
 *
 * Responses are wrapped in an object -- `{"collection": {...}}` and `{"collections": [...]}`
 * -- rather than being a bare row and a bare array, matching MemoController for the reason it
 * gives: the list needs somewhere to echo the filters it answered for, which a top-level JSON
 * array has nowhere to put, and a named key tells you which route produced the body.
 *
 * **This controller carries more 4xx translation than MemoController, and that is where it
 * belongs.** The repository answers three-state -- the row, `false` for a name already taken,
 * `null` for no such collection -- because at the SQL layer none of those is an error: a
 * unique violation is Postgres refusing a write, and an UPDATE matching nothing is an ordinary
 * statement. Turning them into 422 and 404 is what an HTTP layer is for, and doing it here
 * keeps CollectionService free of status codes.
 */
final class CollectionController extends Controller
{
    public function __construct(private readonly CollectionService $collections) {}

    public function index(ListCollectionsRequest $request): JsonResponse
    {
        $collections = $this->collections->list(
            $request->searchQuery(),
            $request->timeWindow(),
            $request->limit(),
        );

        return response()
            ->json([
                'collections' => array_map(
                    static fn (Collection $collection): array => $collection->toArray(),
                    $collections,
                ),

                // The filters the rows came back for, echoed for the reason MemoController's
                // index echoes its own: the grid's search is debounced, so a response can
                // arrive after the box has moved on, and the caption has to name the filter
                // the cards are the answer to rather than whatever is currently typed.
                //
                // Echoed as sent rather than as the normalised instants -- the caption reads
                // in the user's own timezone, and TimeWindow's UTC normalisation exists for
                // comparing rows.
                'query' => $request->searchQuery(),
                'from' => $request->validated()['from'] ?? null,
                'to' => $request->validated()['to'] ?? null,
            ])
            // Same reasoning as the memo list: the frontend re-fetches this whenever a memo is
            // filed or unfiled, because a card's count and its labels change without the
            // collection row itself being touched. A cached response would show a count that
            // is one behind, which is the exact thing that reads as the move not having
            // worked.
            ->header('Cache-Control', 'no-store');
    }

    /**
     * Create a collection.
     *
     * 201 with the stored row, so the frontend can prepend the card and immediately file a
     * memo into it without a follow-up GET -- the id is what it needs, and the count and
     * labels come back as 0 and `[]`, which is what a new collection is.
     */
    public function store(SaveCollectionRequest $request): JsonResponse
    {
        $collection = $this->collections->create($request->name());

        if ($collection === false) {
            throw self::nameTaken($request->name());
        }

        return response()->json(
            ['collection' => $collection->toArray()],
            Response::HTTP_CREATED,
        );
    }

    /**
     * Rename a collection.
     *
     * The two failures answer differently and must: a taken name is a 422 about the field the
     * user just typed, and a missing collection is a 404 about the thing they were looking at.
     * Collapsing them -- which one `if (! $renamed)` would do -- tells somebody who typed a
     * duplicate name that their collection has disappeared.
     *
     * A rename to the collection's *own* name is not a duplicate. The unique index is over
     * `lower(btrim(name))` and the row being updated is the row that holds the value, so
     * Postgres compares it against itself and permits it -- so re-submitting an unchanged
     * name is a successful no-op rather than a 422, which is what a user editing a field and
     * pressing save expects.
     */
    public function update(SaveCollectionRequest $request, string $collection): JsonResponse
    {
        $renamed = $this->collections->rename($collection, $request->name());

        if ($renamed === false) {
            throw self::nameTaken($request->name());
        }

        if ($renamed === null) {
            abort(Response::HTTP_NOT_FOUND, 'That collection no longer exists. Refresh and try again.');
        }

        return response()->json(['collection' => $renamed->toArray()]);
    }

    /**
     * Delete a collection. The memos it held become fast memos again.
     *
     * 204, because there is nothing useful to answer with: the collection is gone, and the
     * memos that were in it are described by the memo list rather than by this route. The
     * frontend re-fetches both after this, which it has to do anyway -- the strip has grown
     * however many memos the collection was holding.
     *
     * Not 200 with a count of what was released. That number would be interesting exactly
     * once and would have to be produced by a second statement before the delete, since
     * `ON DELETE SET NULL` fires inside Postgres where nothing here can observe it.
     */
    public function destroy(string $collection): Response
    {
        if (! $this->collections->delete($collection)) {
            abort(Response::HTTP_NOT_FOUND, 'That collection no longer exists.');
        }

        return response()->noContent();
    }

    /**
     * The 422 for a name that is already in use.
     *
     * A ValidationException rather than a hand-built JsonResponse, because that is what puts
     * the message in the same place every other 422 on this API puts it: Laravel renders the
     * first message into the body's `message` key, which is the field web/src/api/memos.js
     * renders verbatim. A bespoke body would read differently from every other rejected form
     * in the app.
     *
     * Keyed on `name` so the frontend can attach it to the field if it ever wants to, and
     * worded with the name in it because the likeliest cause is a collection the user has
     * already made and forgotten -- and the grid may well be filtered so that it is not on
     * screen.
     */
    private static function nameTaken(string $name): ValidationException
    {
        return ValidationException::withMessages([
            'name' => "You already have a collection called \"{$name}\".",
        ]);
    }
}

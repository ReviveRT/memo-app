<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Http\Requests\ListMemosRequest;
use App\Http\Requests\RetranscribeMemoRequest;
use App\Http\Requests\StoreMemoRequest;
use App\Http\Requests\UpdateMemoRequest;
use App\Services\Memos\Memo;
use App\Services\Memos\MemoService;
use Illuminate\Http\JsonResponse;
use Symfony\Component\HttpFoundation\Response;

/**
 * HTTP in, HTTP out. No SQL, and no decisions about what a memo is.
 *
 * Both responses are wrapped in an object -- {"memo": {...}} and {"memos": [...]}
 * -- rather than being a bare row and a bare array. That was done for a search that did
 * not exist yet, and the room got used: the list now carries "query" alongside "memos",
 * which a top-level JSON array had nowhere to put and which cost no change to the type
 * of anything the frontend already read. The two keys are named rather than a shared
 * "data" so that a response tells you which route produced it.
 *
 * Almost no 4xx handling here. Validation failures are raised by the FormRequests and
 * rendered as 422 JSON by bootstrap/app.php's unconditional shouldRenderJsonWhen,
 * and a database that is down is a 500 -- MEMO-17 owns failure UX, and inventing a
 * second, different answer for it here would be the thing that task then has to
 * undo. An unwritable audio volume takes the same 500, and deliberately: a rejected
 * recording is something the person recording can fix and a volume they cannot mount
 * is not, which is the distinction App\Exceptions\StorageException exists to draw.
 *
 * The one exception is update()'s 404, and it is here rather than in the service because
 * "no row matched" is not an error at any layer below HTTP -- an UPDATE that changes
 * nothing is a perfectly ordinary statement. Turning that into a status code is precisely
 * this class's job.
 */
final class MemoController extends Controller
{
    public function __construct(private readonly MemoService $memos) {}

    /**
     * One route, two accepted bodies: a typed memo as JSON, or a recording as
     * multipart/form-data (MEMO-10).
     *
     * The branch is on which of the two the request carried rather than on the
     * Content-Type, because that is the question the rules already answered -- and the
     * two cannot both be present, which StoreMemoRequest refuses rather than resolves.
     * Everything after the branch is identical, which is the point of putting them on
     * one route: the same 201, carrying the same object, and a frontend that prepends
     * it to the same list either way.
     */
    public function store(StoreMemoRequest $request): JsonResponse
    {
        $audio = $request->audio();

        // The language goes only to the audio path. A typed memo is never transcribed,
        // so a language on one would describe a decode that never happens -- the field
        // is accepted on both for the reason StoreMemoRequest gives, and dropped here.
        $memo = $audio === null
            ? $this->memos->createFromText((string) $request->text())
            : $this->memos->createFromAudio($audio, $request->language());

        // 201, and the body is the stored row rather than an id to go and fetch:
        // the client needs status and created_at to render the memo as pending
        // immediately, and both are decided by the database.
        return response()->json(['memo' => $memo->toArray()], Response::HTTP_CREATED);
    }

    /**
     * Move a memo into a collection, or back out of one.
     *
     * Answers with the whole memo rather than with an acknowledgement, which is the same
     * choice store() makes and for a stronger reason here: the frontend reconciles its list
     * by id (MEMO-18), so a route that returns the row in its new state needs no follow-up
     * GET and cannot leave the client holding a memo whose `collection_id` disagrees with
     * the database.
     *
     * 200 rather than 201 -- nothing was created -- and 404 when the memo does not exist or
     * when the collection named does not. The service cannot tell those two apart and
     * deliberately does not try; the message names both, because the client has the same
     * one thing to do about either.
     *
     * $memo arrives as a string rather than as a model. There is no Eloquent in this
     * project, so there is no implicit route binding to lean on -- the uuid shape is
     * enforced by `whereUuid` on the route, and existence is answered by the UPDATE itself
     * rather than by a SELECT that would only race it.
     */
    public function update(UpdateMemoRequest $request, string $memo): JsonResponse
    {
        $updated = null;

        // Two independent writes rather than one UPDATE with two SET clauses, and the order
        // is arbitrary because they touch different columns. Each answers with the whole memo,
        // so whichever runs last is the state to return -- which is why $updated is
        // overwritten rather than merged. A body carrying both is one request and two
        // statements; that is a round trip saved for the client and no transaction needed,
        // since neither statement can leave the row in a state the other cares about.
        if ($request->movesCollection()) {
            $updated = $this->memos->moveToCollection($memo, $request->collectionId());

            if ($updated === null) {
                abort(
                    Response::HTTP_NOT_FOUND,
                    'That memo or collection no longer exists. Refresh and try again.',
                );
            }
        }

        if ($request->renames()) {
            $updated = $this->memos->rename($memo, $request->title());

            if ($updated === null) {
                abort(
                    Response::HTTP_NOT_FOUND,
                    'That memo no longer exists. Refresh and try again.',
                );
            }
        }

        // Unreachable: UpdateMemoRequest refuses a body that asks for neither, so one of the
        // two branches above has run. Asserted rather than assumed, because the alternative
        // is a null dereference one edit to the rules away.
        if ($updated === null) {
            abort(Response::HTTP_UNPROCESSABLE_ENTITY, 'That request asked for no change.');
        }

        return response()->json(['memo' => $updated->toArray()]);
    }

    /**
     * Send a failed memo back to the worker (MEMO-17).
     *
     * **The one 409 in this project, and the argument for it is what the client does next.**
     * Every other write here answers a request naming something that is not there with a 404,
     * and this route has that case too -- but it also has a second one that is not it: the
     * memo is right there and is simply not failed, because the worker finished it, or
     * because the other tab pressed Retry a second ago. Flattening that into a 404 would put
     * "That memo no longer exists" under a memo the user is looking at, and the frontend
     * renders these sentences verbatim. 409 says the request was well-formed and the
     * resource's state is what refused it, which is exactly true.
     *
     * The message names the state it found, so the answer to "why did nothing happen" is in
     * the response rather than in a second request. A 200 for the refused case was the other
     * candidate -- retry-as-idempotent, "it is queued or better, which is what you wanted" --
     * and it was rejected because it is not idempotent underneath: `ready` and `processing`
     * are refused for real reasons (see MemoRepository::requeue), and reporting a refusal as
     * a success would hide a `ready` memo's title being spared, not describe it.
     *
     * No body, and no FormRequest. There is nothing to send: the id is in the path and the
     * new state is not the client's to choose. ValidateJsonBody lets a bodyless POST through,
     * and one carrying broken JSON is still a 400 -- which is right, since a client sending a
     * body here has misunderstood the route.
     */
    public function retry(string $memo): JsonResponse
    {
        $outcome = $this->memos->retry($memo);

        if ($outcome->memo === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo no longer exists.');
        }

        if (! $outcome->requeued) {
            abort(
                Response::HTTP_CONFLICT,
                "Only a failed memo can be retried, and this one is {$outcome->memo->status}."
                    .' Refresh to see where it got to.',
            );
        }

        // 200 with the whole memo, like every other write on this resource, and it earns its
        // keep here more than most: the row comes back `queued`, which is what flips the
        // frontend's `pending` and restarts the poll that will show the retry finishing. A
        // 204 would leave the card sitting on `failed` until something else happened to
        // refresh it.
        return response()->json(['memo' => $outcome->memo->toArray()]);
    }

    /**
     * Decode a voice memo again, in a language the user names.
     *
     * **Why this is not `retry` with a parameter.** Retry's contract is "this failed, try
     * again", and its `status = 'failed'` guard is the whole of its safety. The memo this
     * route is called about is usually `ready`: a Romanian recording transliterated into
     * Cyrillic is a *successful* job by every measure the worker has, and it is the user
     * who can see it is wrong. Widening Retry to accept `ready` would mean a Retry click
     * could discard a transcript somebody is reading. MemoRepository::retranscribe has
     * the three conditions and what each refuses.
     *
     * The 409 is the same shape as Retry's and reachable for more reasons: a text memo has
     * no audio to decode, and a memo already `queued` or `processing` is mid-flight with a
     * worker possibly holding its fence token. Both name the state they found, because
     * these sentences reach the user verbatim.
     *
     * 200 with the whole memo, for the reason Retry gives -- the row comes back `queued`
     * with `transcript` cleared, which is what flips the frontend to pending and restarts
     * the poll that will show the new transcript arriving. A client that got a 204 here
     * would sit on the old, wrong transcript with no indication anything was happening.
     */
    public function retranscribe(RetranscribeMemoRequest $request, string $memo): JsonResponse
    {
        $outcome = $this->memos->retranscribe($memo, $request->language());

        if ($outcome->memo === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo no longer exists.');
        }

        if (! $outcome->requeued) {
            // Two distinct refusals, and the difference is worth spelling out rather than
            // reporting both as "wrong state": one is permanent and the other resolves on
            // its own in a second or two.
            $reason = $outcome->memo->source === Memo::SOURCE_TEXT
                ? 'Only a voice memo can be transcribed again, and this one was typed.'
                : "A memo can only be transcribed again once it has finished, and this one is {$outcome->memo->status}."
                    .' Refresh to see where it got to.';

            abort(Response::HTTP_CONFLICT, $reason);
        }

        return response()->json(['memo' => $outcome->memo->toArray()]);
    }

    /**
     * Delete a memo, its recording, and any reminders hanging off it.
     *
     * **200 with the deleted memo rather than 204.** Every other write on this resource
     * answers with the row, and the frontend reconciles one shape by id -- so a body here
     * costs nothing and means the client can report *what* it removed rather than only that
     * something was removed. It also gives the undo-shaped conversation somewhere to start,
     * if that is ever wanted; a 204 throws the row away at exactly the moment it is last
     * available. `DELETE /api/collections/{id}` answers 204 and is the odd one out for a
     * reason that does not apply here: a collection's contents survive it, so the interesting
     * fact about that request is what happened to the *memos*, and the response cannot say.
     *
     * 404 when there is no such memo, which makes this non-idempotent in its status code:
     * deleting the same memo twice gives 200 then 404. That is the right answer rather than a
     * blanket 204, because the second request is the client telling us about a memo it thinks
     * exists -- two tabs, or a stale list -- and it should find out that it does not.
     */
    public function destroy(string $memo): JsonResponse
    {
        $deleted = $this->memos->delete($memo);

        if ($deleted === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo no longer exists.');
        }

        return response()->json(['memo' => $deleted->toArray()]);
    }

    public function index(ListMemosRequest $request): JsonResponse
    {
        $memos = $this->memos->list($request->memoQuery());

        return response()
            ->json([
                'memos' => array_map(
                    static fn (Memo $memo): array => $memo->toArray(),
                    $memos,
                ),

                // The filter the rows came back for, echoed because the client cannot
                // otherwise tell which query a response belongs to -- searching is
                // debounced and polled, so a response can arrive after the box has moved
                // on, and the frontend captions the list from this rather than from what
                // is currently typed. null when unfiltered, so the key is always present
                // and always means the same thing; this is the room the envelope was added
                // for.
                'query' => $request->searchQuery(),

                // The other three filters, echoed for the same reason and added as
                // siblings rather than folded into `query`. `query` keeps its type and its
                // meaning -- a string or null -- because the frontend already reads it,
                // and turning it into an object would be a breaking change to buy tidiness.
                //
                // These are echoed as the client sent them rather than as the normalised
                // instants the window was built from. The caption says "19 Jul - 23 Jul" in
                // the reader's own timezone, and a UTC instant is not what it needs to say
                // that; TimeWindow's normalisation is for comparing rows, not for display.
                'from' => $request->validated()['from'] ?? null,
                'to' => $request->validated()['to'] ?? null,
                'collection' => $request->validated()['collection'] ?? null,
            ])
            // The list is polled every couple of seconds while anything is still
            // transcribing (MEMO-18), and the whole point of each tick is that the
            // answer has changed. A conditional-request revalidation would be
            // reasonable; a cached response is not, and no-store is what keeps an
            // intermediary from making that choice for us.
            ->header('Cache-Control', 'no-store');
    }
}

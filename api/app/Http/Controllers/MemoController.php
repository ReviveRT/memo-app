<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Http\Requests\ListMemosRequest;
use App\Http\Requests\StoreMemoRequest;
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
 * No 4xx handling here. Validation failures are raised by the FormRequests and
 * rendered as 422 JSON by bootstrap/app.php's unconditional shouldRenderJsonWhen,
 * and a database that is down is a 500 -- MEMO-17 owns failure UX, and inventing a
 * second, different answer for it here would be the thing that task then has to
 * undo. An unwritable audio volume takes the same 500, and deliberately: a rejected
 * recording is something the person recording can fix and a volume they cannot mount
 * is not, which is the distinction App\Exceptions\StorageException exists to draw.
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

        $memo = $audio === null
            ? $this->memos->createFromText((string) $request->text())
            : $this->memos->createFromAudio($audio);

        // 201, and the body is the stored row rather than an id to go and fetch:
        // the client needs status and created_at to render the memo as pending
        // immediately, and both are decided by the database.
        return response()->json(['memo' => $memo->toArray()], Response::HTTP_CREATED);
    }

    public function index(ListMemosRequest $request): JsonResponse
    {
        $query = $request->searchQuery();

        $memos = $this->memos->recent($query, $request->limit());

        return response()
            ->json([
                'memos' => array_map(
                    static fn (Memo $memo): array => $memo->toArray(),
                    $memos,
                ),

                // The filter the rows came back for, echoed because the client cannot
                // otherwise tell which query a response belongs to -- searching is
                // debounced and polled, so a response can arrive after the box has moved
                // on, and the frontend discards a stale one by comparing this. null when
                // unfiltered, so the key is always present and always means the same
                // thing; this is the room the envelope was added for.
                'query' => $query,
            ])
            // The list is polled every couple of seconds while anything is still
            // transcribing (MEMO-18), and the whole point of each tick is that the
            // answer has changed. A conditional-request revalidation would be
            // reasonable; a cached response is not, and no-store is what keeps an
            // intermediary from making that choice for us.
            ->header('Cache-Control', 'no-store');
    }
}

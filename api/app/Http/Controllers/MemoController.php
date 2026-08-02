<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Contracts\AudioStorage;
use App\Http\Requests\ListMemosRequest;
use App\Http\Requests\StoreMemoRequest;
use App\Http\Requests\UpdateMemoRequest;
use App\Http\Responses\AudioFileResponse;
use App\Http\Rules\SniffedAudioType;
use App\Services\Memos\Memo;
use App\Services\Memos\MemoService;
use Illuminate\Http\JsonResponse;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\ResponseHeaderBag;

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
    /**
     * How long a browser may keep a recording it has already fetched, in seconds.
     *
     * A year, which is the conventional spelling of "forever" for a URL whose bytes cannot
     * change -- see audio() for why they cannot. The number is only reachable by a client that
     * keeps a memo's id, and the id stops resolving the moment the memo is deleted.
     */
    private const PLAYBACK_MAX_AGE = 31_536_000;

    /**
     * AudioStorage is here for `audio()` alone, and MemoService::audioFor has the argument for
     * why it is not behind the service the way every other blob operation is.
     */
    public function __construct(
        private readonly MemoService $memos,
        private readonly AudioStorage $storage,
    ) {}

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

        // Last of the three, so a body carrying both a title and a transcript returns the row
        // with both applied. The order is otherwise arbitrary -- they touch different columns
        // -- but "whichever ran last is what we return" only holds if the last one ran after
        // the others, and a reader should not have to check that.
        if ($request->correctsTranscript()) {
            $updated = $this->memos->correctTranscript($memo, $request->transcript());

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

    /**
     * One memo by id.
     *
     * **The route exists for the ask widget, and the case it answers is not an edge one.** A
     * citation names a memo the answer was built from; ask reads the whole table, while the
     * screen the widget floats over holds only the unfiled memos matching whatever filter is
     * active. So a cited memo that has been filed into a collection is one the client has no
     * copy of -- and `GET /memos` cannot fetch it either, because that route filters and this
     * is a lookup by identity.
     *
     * `no-store`, for a weaker version of the list's reason. A memo is edited from the card
     * this response opens -- renamed, its transcript corrected, moved -- and it is written by
     * the worker while it transcribes. Nothing here is immutable the way a recording's bytes
     * are (see `audio()`), so there is nothing for a cache to be right about for long.
     *
     * 404 with the same sentence the writes use for a memo that is not there, because the
     * client has the same one thing to do about it.
     */
    public function show(string $memo): JsonResponse
    {
        $found = $this->memos->find($memo);

        if ($found === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo no longer exists.');
        }

        return response()
            ->json(['memo' => $found->toArray()])
            ->header('Cache-Control', 'no-store');
    }

    /**
     * Play back the original recording (MEMO-23).
     *
     * **Range support is the feature, not a refinement of it.** Safari refuses to play audio
     * from an endpoint that answers a `Range` request with the whole file, and without it
     * nothing can seek anywhere: dragging the scrubber asks for a byte offset, and a server
     * that cannot answer one leaves the player to download the file again from the start.
     *
     * BinaryFileResponse is what does it, and the reason it is that rather than an
     * `X-Accel-Redirect` handed to Caddy is recorded in NOTES.md with what was measured. The
     * short version: FrankenPHP has no X-Accel-Redirect of its own -- checked against the
     * shipped binary, the string is not in it -- so the accelerated path is a Caddy
     * `intercept` block injected through a compose environment variable, where a typo stops
     * the server from starting and where no test in this suite can reach the bytes. Symfony's
     * range handling is framework code rather than the hand-rolled parsing the task warns
     * against, the cap on a recording is 12 MiB (config/memo.php), and the api container runs
     * a threaded server so a client holding one of these does not stall the status polls
     * behind it -- see api/Dockerfile, which names a streaming audio response as the reason it
     * chose FrankenPHP over `php -S`, before there was anything here to stream.
     *
     * That path is still one line away if it is ever wanted, and it needs no change here:
     * Symfony emits `X-Accel-Redirect` from this same response object once
     * `BinaryFileResponse::trustXSendfileTypeHeader()` is on and the request carries
     * `X-Sendfile-Type`. It is deliberately *off*, and not merely unconfigured -- that
     * setting trusts a header from the **request**, so with it on and no Caddy block to
     * inject one, any client could send `X-Sendfile-Type: x-accel-redirect` and get back an
     * empty 200 carrying the absolute path of the file on the volume.
     *
     * **Two 404s with different sentences, and the second one is not paranoia.** A memo with
     * no recording is the ordinary case -- every typed memo -- and reads as "there is nothing
     * to play". A memo whose row names a blob the volume does not have is a stack that has
     * lost data: `docker compose down -v` between recording and playing does it, and so does
     * a restore of the database without the volume. Flattening both into one sentence would
     * make the second look like the first, and the first is not something to investigate.
     *
     * Neither is a 500. The row is intact and the API is working; what is missing is bytes it
     * never promised in the response it is answering. StorageException is left to become a
     * 500, which is the distinction MemoController's class docblock draws: a caller can do
     * nothing about an unmounted volume, and that is not this.
     */
    public function audio(string $memo): AudioFileResponse
    {
        $audio = $this->memos->audioFor($memo);

        if ($audio === null) {
            abort(Response::HTTP_NOT_FOUND, 'That memo has no recording to play.');
        }

        $path = $this->storage->localPath($audio->key);

        if ($path === null) {
            abort(
                Response::HTTP_NOT_FOUND,
                'The recording for that memo is no longer on the audio volume.',
            );
        }

        // A BinaryFileResponse in all but one respect -- see AudioFileResponse for the
        // Content-Length an unsatisfiable range would otherwise go out promising.
        $response = new AudioFileResponse($path);

        // Explicit, so it wins over BinaryFileResponse's own fallback -- which would sniff
        // the file again with finfo on every single range request a scrub produces. It is
        // also the right answer rather than merely the cheap one: what is stored is what
        // SniffedAudioType read off these same bytes at upload, and that rule is the one
        // that decided they were a recording at all.
        $response->headers->set('Content-Type', $this->playbackType($audio->mimeType));

        // Belt and braces on that. This app serves user-supplied bytes from its own origin,
        // and the whole defence is that `audio_mime` can only be a value the upload rule
        // vouched for -- so this says "do not go looking for a better answer than the one
        // in the header" to a browser that would otherwise sniff its way to text/html.
        $response->headers->set('X-Content-Type-Options', 'nosniff');

        // inline, so the browser plays it where it is asked to rather than offering to save
        // it. The filename is the last segment of the storage key -- `{memo id}.{ext}` --
        // which is not a name anybody chose but is the one that answers "which blob is this?"
        // when a recording has been saved out of a browser and needs matching back to a row.
        //
        // **basename, and it is not defensive tidying.** Symfony refuses a disposition
        // filename containing `/` with an InvalidArgumentException, which is a 500 rather
        // than a bad header. Keys are flat today, but LocalAudioStorage handles nested ones
        // and pins the directory modes three levels down, and MemoService::createFromAudio
        // says date-sharding is available whenever this volume holds enough files to want it.
        // Taking that option would otherwise turn every playback request into a 500, at the
        // point furthest from the change that caused it.
        $response->setContentDisposition(
            ResponseHeaderBag::DISPOSITION_INLINE,
            basename($audio->key),
        );

        // The opposite of the list's `no-store`, and both are right. A memo's recording is
        // written once and never rewritten: `audio_path` is set by the INSERT and no statement
        // in MemoRepository updates it, and the edits a client *can* make -- the title, the
        // transcript, which collection it is in -- all leave the bytes alone. So this URL
        // either answers with the same file forever or, once the memo is deleted, stops
        // existing. That is what `immutable` means, and it is what keeps a scrub from
        // re-fetching ranges the browser already has.
        //
        // private, because there is no authentication in this app (README, Assumptions) and
        // a shared cache holding one user's recordings is not a thing to leave to a default.
        $response->setPrivate();
        $response->setMaxAge(self::PLAYBACK_MAX_AGE);
        $response->setImmutable();

        // Last-Modified comes from the file's mtime, set by BinaryFileResponse's own
        // constructor default. It is not decoration here: Symfony validates `If-Range`
        // against it, which is what lets a player resume a seek it started before the
        // response it was reading was replaced. `Accept-Ranges: bytes` is added in
        // prepare(), and the 206, the `Content-Range` and the 416 for a range past the end
        // of the file all come from the same place.
        return $response;
    }

    /**
     * What to serve a recording as: the type stored with it, or octet-stream.
     *
     * The stored value cannot be anything but an allowed type today -- SniffedAudioType is
     * what wrote it, and it refuses everything not on that list -- so this re-check is about
     * rows this build did not write: an older upload path, a restored dump, a row inserted by
     * hand. `application/octet-stream` for those rather than passing them through, because
     * the one thing that must not happen is a memo serving `text/html` from this origin.
     *
     * Reusing the rule's constant rather than restating it, so the list of things this app
     * will serve cannot drift from the list it will accept.
     */
    private function playbackType(?string $stored): string
    {
        return $stored !== null && in_array($stored, SniffedAudioType::ALLOWED, true)
            ? $stored
            : 'application/octet-stream';
    }
}

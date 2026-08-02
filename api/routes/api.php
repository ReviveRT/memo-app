<?php

/*
|--------------------------------------------------------------------------
| API Routes
|--------------------------------------------------------------------------
|
| Every public route in the project. Registered in bootstrap/app.php under the
| default apiPrefix of 'api', so the paths here are served at /api/*.
|
*/

declare(strict_types=1);

use App\Http\Controllers\AskController;
use App\Http\Controllers\CollectionController;
use App\Http\Controllers\HealthController;
use App\Http\Controllers\MemoController;
use App\Http\Controllers\ReminderController;
use Illuminate\Support\Facades\Route;

Route::get('/health', [HealthController::class, 'show'])->name('health');

// Ask my memos (MEMO-24). The one route here that does not answer for itself: it proxies to
// the `ai-api` container, which retrieves a few memos through the same full-text index
// `?q=` uses and has a local model answer from them.
//
// POST rather than GET, and it is not only that a question can be long. A GET would put
// somebody's question in a URL -- into the api container's access log, into the browser's
// history, into whatever sits in front of this later -- and a question about your own memos
// is at least as private as their contents. The body is also what makes `?q=` and this route
// visibly different things rather than two spellings of search.
//
// Not under /memos, even though every memo it reads is one, because it is not a sub-resource
// of anything: it answers across the whole table and produces no memo. `/api/ask` says what it
// is, which is the same reasoning `/api/health` gets.
//
// **The one route in this file that streams**, and the only one that is not JSON except for
// the audio bytes. It answers `application/x-ndjson` -- one JSON object per line -- so the
// browser can render words as the model produces them rather than after forty seconds of
// nothing. AskController has why a failure partway through cannot be a status code.
Route::post('/ask', [AskController::class, 'store'])->name('ask');

// The memo collection (MEMO-06). The intended shape for MEMO-11 is a second
// accepted body on this same POST rather than a /api/memos/audio of its own: both
// produce one memo row and differ only in which of `transcript` and `audio_path`
// starts out set. Nothing here forces that -- StoreMemoRequest would relax
// `required` on `text` to required_without:audio -- but it is why the route is
// named for the collection and not for the text case.
//
// The list also takes `?q=` (MEMO-19), and now `?from=`, `?to=` and `?collection=` as well.
// All four are parameters on this same route rather than routes of their own, because each
// returns the same rows in the same order and the frontend swaps between them as fast as
// somebody can type -- separate routes would mean several response shapes to reconcile for
// one list. `?collection=none` is the fast strip; see ListMemosRequest for why the scope is
// one parameter with three readings instead of two that can contradict each other.
//
Route::get('/memos', [MemoController::class, 'index'])->name('memos.index');
Route::post('/memos', [MemoController::class, 'store'])->name('memos.store');

// One memo by id. The lookup the list route cannot do: `GET /memos` answers a *filter*, and
// the ask widget's citations name memos by identity -- a cited memo filed into a collection
// is not in any list the screen behind the widget would ask for. MemoController::show has the
// longer version.
//
// Placed between the list and the recording rather than beside the writes, because it is a
// read of the same resource -- and above the whereUuid paragraph below, so it says its own
// constraint rather than borrowing that one's.
Route::get('/memos/{memo}', [MemoController::class, 'show'])
    ->whereUuid('memo')
    ->name('memos.show');

// The original recording, with byte ranges (MEMO-23). The one route in this file that does
// not answer JSON, and the one whose response the frontend never fetches: it is the `src` of
// an <audio> element, so the browser issues the requests itself and expects a 206 to a Range
// header. MemoController::audio has what serves them, and why it is not Caddy.
//
// A sub-resource rather than a field on the memo, because it is bytes and a memo is JSON --
// there is no shape of `GET /api/memos` that can carry a recording. Under the memo rather
// than at a `/api/audio/{key}` of its own for the reason App\Contracts\AudioStorage gives: a
// storage key is not a client's to hold, so the memo id it already has is what addresses
// this.
//
// GET only, and Laravel registers the matching HEAD for free. Symfony answers it from this
// same method with the headers and no body, which is the correct thing for `curl -I` and for
// anything sizing a file before fetching it. Not, as it turns out, what a browser does: the
// Chrome media element opens with a ranged GET and issues no HEAD at all -- measured over
// CDP, one request for one playback. HEAD is here because it is free and right, not because
// the feature depends on it.
//
// whereUuid for the reason spelled out over the writes below -- there is no implicit route
// binding in this project, so an id that is not a uuid would otherwise reach Postgres and
// come back as a 500. It is placed here, ahead of that paragraph rather than under it,
// because this is a read and the writes are grouped together after it.
Route::get('/memos/{memo}/audio', [MemoController::class, 'audio'])
    ->whereUuid('memo')
    ->name('memos.audio');

// Filing a memo into a collection, taking it back out, or renaming it. PATCH rather than PUT
// because the body is a change and not a replacement -- a PUT carrying only `collection_id`
// would be asking the API to discard the transcript.
//
// Three fields on one route rather than a `/memos/{memo}/title` and a `/transcript` of their
// own, because all three are small edits to the same row answering with the same shape, and the
// client already has one function for "PATCH a memo and merge the result". UpdateMemoRequest has
// the argument for which of a memo's columns a client may write, and why `transcript` -- which
// this note used to name as the example of one they may not -- is now among them.
//
// whereUuid on every id below, and it is doing real work rather than tidying: there is no
// Eloquent in this project (MEMO-05) and therefore no implicit route binding, so without it
// `/api/memos/not-a-uuid` would reach the controller, be handed to Postgres, and come back as
// a 500 from `invalid input syntax for type uuid`. Constrained, it never matches a route and
// answers the 404 that a nonexistent memo should.
Route::patch('/memos/{memo}', [MemoController::class, 'update'])
    ->whereUuid('memo')
    ->name('memos.update');

// Retrying a failed memo (MEMO-17). POST rather than PATCH, and a path segment rather than a
// field in the body, because this is not an edit to the memo -- it is an action taken *about*
// it, and the state it produces is not the client's to name. A `PATCH {"status":"queued"}`
// would invite the next caller to try `{"status":"ready"}`, which is the worker's alone.
//
// Here rather than up beside `POST /memos`, and the placement is doing two small jobs: this is
// a write on one existing memo like the two it now sits between, and it is under the whereUuid
// paragraph above, which says "every id below" and would otherwise not have covered it.
//
// Not idempotent, deliberately: a second press answers 409 rather than shrugging. See
// MemoController::retry for why "it is already queued" is worth saying out loud, and
// MemoRepository::requeue for why `processing` and `ready` are refused rather than tolerated.
Route::post('/memos/{memo}/retry', [MemoController::class, 'retry'])
    ->whereUuid('memo')
    ->name('memos.retry');

// Deleting a memo takes its recording and its reminders with it. The reminders go through
// `ON DELETE CASCADE` inside Postgres; the audio blob is unlinked by MemoService, which has
// the argument for why the row goes first and why a failed unlink is still a successful
// delete.
//
// Answers 200 with the memo it removed rather than 204 -- see MemoController::destroy for why
// this differs from the collections delete beside it.
Route::delete('/memos/{memo}', [MemoController::class, 'destroy'])
    ->whereUuid('memo')
    ->name('memos.destroy');

// --- Collections -----------------------------------------------------------
//
// The grid of named collections. `index` takes the same `?q=`, `?from=` and `?to=` the memo
// list takes, because the brief asks for one filter that works the same in both places --
// ListCollectionsRequest is explicit that the parameters are spelled identically on purpose.
//
// No `show`. A collection's *contents* are memos, and those come from
// `GET /api/memos?collection=<id>` -- which already carries the filters, the limit and the
// in-flight pin. A second route answering with nested memos would be a second list to keep in
// step with the first.
Route::get('/collections', [CollectionController::class, 'index'])->name('collections.index');
Route::post('/collections', [CollectionController::class, 'store'])->name('collections.store');
Route::patch('/collections/{collection}', [CollectionController::class, 'update'])
    ->whereUuid('collection')
    ->name('collections.update');

// Deleting a collection does not delete its memos -- `ON DELETE SET NULL` on
// `memos.collection_id` returns them to the fast strip. That lives in the constraint
// (003_collections_and_reminders.sql) rather than in the controller, so it holds for every
// deleter of this table and not only for this route.
Route::delete('/collections/{collection}', [CollectionController::class, 'destroy'])
    ->whereUuid('collection')
    ->name('collections.destroy');

// --- Reminders -------------------------------------------------------------
//
// Creating one is scoped under the memo, because that is the thing it needs naming.
// Acknowledging and deleting one are not: the reminder's own id identifies it, and a
// `/memos/{memo}/reminders/{reminder}` would put a segment in the path that nothing reads and
// nothing checks for agreement with the reminder.
//
// All three writes answer `{"memo": {...}}` rather than the reminder -- see ReminderController
// for why, and for what it costs.
//
// The index is the one route that answers reminders, and it is here for a reason the writes
// are not: it feeds the browser's delivery loop, which has to know about a reminder on a memo
// that is nowhere on screen. The fast strip holds only unfiled memos, so without this a
// reminder set and then filed into a collection would silently never fire.
Route::get('/reminders', [ReminderController::class, 'index'])->name('reminders.index');
Route::post('/memos/{memo}/reminders', [ReminderController::class, 'store'])
    ->whereUuid('memo')
    ->name('memos.reminders.store');

// PATCH with no body: it marks the reminder delivered, and the timestamp is `now()` in SQL
// rather than anything the client sends. A browser's clock has no business writing the column
// used to judge whether reminders arrive on time.
Route::patch('/reminders/{reminder}', [ReminderController::class, 'update'])
    ->whereUuid('reminder')
    ->name('reminders.update');

Route::delete('/reminders/{reminder}', [ReminderController::class, 'destroy'])
    ->whereUuid('reminder')
    ->name('reminders.destroy');

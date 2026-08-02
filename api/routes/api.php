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

use App\Http\Controllers\CollectionController;
use App\Http\Controllers\HealthController;
use App\Http\Controllers\MemoController;
use App\Http\Controllers\ReminderController;
use Illuminate\Support\Facades\Route;

Route::get('/health', [HealthController::class, 'show'])->name('health');

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
// Still to attach here: the audio range endpoint (MEMO-23).
Route::get('/memos', [MemoController::class, 'index'])->name('memos.index');
Route::post('/memos', [MemoController::class, 'store'])->name('memos.store');

// Filing a memo into a collection, taking it back out, or renaming it. PATCH rather than PUT
// because the body is a change and not a replacement -- a PUT carrying only `collection_id`
// would be asking the API to discard the transcript.
//
// Two fields on one route rather than a `/memos/{memo}/title` of its own, because both are
// small edits to the same row answering with the same shape, and the client already has one
// function for "PATCH a memo and merge the result". UpdateMemoRequest has the argument for why
// `title` is the only *content* a client may write and `transcript` is not.
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

// Beside retry rather than merged with it: same verb and shape, different question. Retry
// asks "this failed, try again"; this one asks "the language was wrong, do it again in
// Romanian" about a memo that usually succeeded. MemoController::retranscribe has why one
// route could not safely answer both.
Route::post('/memos/{memo}/retranscribe', [MemoController::class, 'retranscribe'])
    ->whereUuid('memo')
    ->name('memos.retranscribe');

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

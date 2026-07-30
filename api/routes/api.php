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

use App\Http\Controllers\HealthController;
use App\Http\Controllers\MemoController;
use Illuminate\Support\Facades\Route;

Route::get('/health', [HealthController::class, 'show'])->name('health');

// The memo collection (MEMO-06). The intended shape for MEMO-11 is a second
// accepted body on this same POST rather than a /api/memos/audio of its own: both
// produce one memo row and differ only in which of `transcript` and `audio_path`
// starts out set. Nothing here forces that -- StoreMemoRequest would relax
// `required` on `text` to required_without:audio -- but it is why the route is
// named for the collection and not for the text case.
//
// Still to attach here: the search parameter on the list (MEMO-19), the retry
// action (MEMO-17) and the audio range endpoint (MEMO-23).
Route::get('/memos', [MemoController::class, 'index'])->name('memos.index');
Route::post('/memos', [MemoController::class, 'store'])->name('memos.store');

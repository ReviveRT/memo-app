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

// The memo collection (MEMO-06). Both verbs are one route on purpose: MEMO-11 adds
// audio to the same POST as a second accepted body shape rather than as a
// /api/memos/audio of its own, because the two produce the same row and differ only
// in which of `transcript` and `audio_path` starts out set.
//
// Still to attach here: the search parameter on the list (MEMO-19), the retry
// action (MEMO-17) and the audio range endpoint (MEMO-23).
Route::get('/memos', [MemoController::class, 'index'])->name('memos.index');
Route::post('/memos', [MemoController::class, 'store'])->name('memos.store');

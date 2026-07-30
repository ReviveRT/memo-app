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
use Illuminate\Support\Facades\Route;

Route::get('/health', [HealthController::class, 'show'])->name('health');

// POST /api/memos and GET /api/memos land here (MEMO-06), then the search
// parameter (MEMO-19), the retry action (MEMO-17) and the audio range endpoint
// (MEMO-23).

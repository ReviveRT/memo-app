<?php

declare(strict_types=1);

use App\Support\Env;

/*
|--------------------------------------------------------------------------
| Memo App
|--------------------------------------------------------------------------
|
| Application settings that are not Laravel's. Defaults mirror
| docker-compose.yml and .env.example -- repeated rather than derived, because
| this config also has to be right when a service is run outside compose.
|
| Nothing outside this file calls env() for these. Anything reading env()
| directly breaks under `php artisan config:cache`, which resolves env() at
| cache time and returns null for it afterwards.
|
| Both values go through App\Support\Env rather than env() directly, so an
| empty string is treated as absent and a non-numeric byte cap fails loudly.
| See that class for why plain env() is not enough here.
|
*/

return [

    // Where audio is written inside the container, on the shared `audio` volume
    // that the Python worker mounts at the same path and reads by key.
    'audio_dir' => Env::string('AUDIO_DIR', '/data/audio'),

    // Byte cap enforced at the API edge (12 MiB). Deliberately looser than the
    // duration cap: a WebM stream from MediaRecorder carries no duration element,
    // so length is enforced in the worker after normalization, not here.
    //
    // Raising this above upload_max_filesize in conf.d/uploads.ini silently
    // re-breaks uploads; GET /api/health reports both numbers side by side and
    // flags the mismatch.
    'max_audio_bytes' => Env::positiveInt('MAX_AUDIO_BYTES', 12 * 1024 * 1024),

];

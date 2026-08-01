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

    // Byte cap for the API edge (12 MiB). Deliberately looser than the duration
    // cap: a WebM stream from MediaRecorder carries no duration element, so
    // length is enforced in the worker after normalization, not here.
    //
    // Measured against real recordings from all three browsers in MEMO-10, rather
    // than taken from the ticket. ffprobe, on files that each decode to
    // completion:
    //
    //   Chrome   WebM   Opus  48 kHz mono   duration=N/A
    //   Firefox  Ogg    Opus  48 kHz mono   duration=3.5745
    //   Safari   MP4    AAC   48 kHz mono   duration=6.252
    //
    // Chrome is the only one of the three carrying no duration, which makes an
    // edge check worse than no check rather than merely incomplete: it would
    // pass on two browsers out of three and be read as enforced, and the one it
    // silently exempts is the most common. Hence MEMO-13 measuring after
    // normalization, where every input has been through ffmpeg and the answer
    // does not depend on who recorded it.
    //
    // Two codecs, not one, for MEMO-13 to normalize -- Safari is AAC.
    //
    // Enforced by StoreMemoRequest, which answers 413 rather than 422 for anything
    // over it, and reported by HealthService next to the two PHP limits that have to
    // stay above it. Those three numbers agreeing is the whole of whether an upload
    // of exactly this size can physically reach a handler.
    //
    // Raising this above upload_max_filesize in conf.d/uploads.ini silently
    // re-breaks uploads; GET /api/health reports both numbers side by side and
    // flags the mismatch.
    'max_audio_bytes' => Env::positiveInt('MAX_AUDIO_BYTES', 12 * 1024 * 1024),

];

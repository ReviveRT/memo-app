<?php

return [

    /*
    |--------------------------------------------------------------------------
    | Default Filesystem Disk
    |--------------------------------------------------------------------------
    |
    | Here you may specify the default filesystem disk that should be used
    | by the framework. The "local" disk, as well as a variety of cloud
    | based disks are available to your application for file storage.
    |
    */

    'default' => env('FILESYSTEM_DISK', 'local'),

    /*
    |--------------------------------------------------------------------------
    | Filesystem Disks
    |--------------------------------------------------------------------------
    |
    | Below you may configure as many filesystem disks as necessary, and you
    | may even configure multiple disks for the same driver. Examples for
    | most supported storage drivers are configured here for reference.
    |
    | Supported drivers: "local", "ftp", "sftp", "s3"
    |
    */

    'disks' => [

        'local' => [
            'driver' => 'local',
            'root' => storage_path('app/private'),

            // false, not the skeleton's true. `serve => true` makes Laravel register
            // routes on this disk, and `php artisan route:list` on the stock
            // skeleton shows both of them:
            //
            //     GET  storage/{path}   storage.local
            //     PUT  storage/{path}   storage.local.upload
            //
            // An unauthenticated PUT that writes into the container is not something
            // this API should expose, and neither route has a consumer: the Vue app
            // talks only to /api/*, and audio is served by MEMO-23 from the `audio`
            // volume through a controller that can enforce Range and ownership.
            // There is no auth anywhere in this project (MEMO-27 lists that as a
            // deliberate cut), so "no route" is the only access control available.
            'serve' => false,

            'throw' => false,
            'report' => false,
        ],

        'public' => [
            'driver' => 'local',
            'root' => storage_path('app/public'),
            'url' => rtrim(env('APP_URL', 'http://localhost'), '/').'/storage',
            'visibility' => 'public',
            'throw' => false,
            'report' => false,
        ],

        's3' => [
            'driver' => 's3',
            'key' => env('AWS_ACCESS_KEY_ID'),
            'secret' => env('AWS_SECRET_ACCESS_KEY'),
            'region' => env('AWS_DEFAULT_REGION'),
            'bucket' => env('AWS_BUCKET'),
            'url' => env('AWS_URL'),
            'endpoint' => env('AWS_ENDPOINT'),
            'use_path_style_endpoint' => env('AWS_USE_PATH_STYLE_ENDPOINT', false),
            'throw' => false,
            'report' => false,
        ],

        /*
        |----------------------------------------------------------------------
        | audio
        |----------------------------------------------------------------------
        |
        | Where recordings live when this deployment keeps them in a bucket.
        | App\Providers\AppServiceProvider picks App\Storage\S3AudioStorage over
        | the local driver on the strength of `bucket` alone being set, so an
        | unconfigured deployment -- local compose, every test -- never touches
        | this and needs none of these variables.
        |
        | Separate from the `s3` disk above rather than reusing it, because two
        | of the settings below are decisions about *recordings* rather than
        | about S3, and inheriting the framework's defaults would get both
        | wrong.
        |
        | Cloudflare R2 is the intended target: same API, 10 GB free, and no
        | egress charge -- which for audio is the whole bill. deploy/README.md
        | has the bucket setup and where each of these values comes from.
        |
        */
        'audio' => [
            'driver' => 's3',
            'key' => env('AUDIO_BUCKET_KEY'),
            'secret' => env('AUDIO_BUCKET_SECRET'),

            // R2 ignores the region and wants the literal 'auto'. Real S3 needs
            // its own, so this is a variable with R2's answer as the default.
            'region' => env('AUDIO_BUCKET_REGION', 'auto'),

            'bucket' => env('AUDIO_BUCKET'),

            // R2's S3 API endpoint, or another provider's. Absent for real AWS,
            // where the SDK derives it from the region.
            'endpoint' => env('AUDIO_BUCKET_ENDPOINT'),

            // No `url`, deliberately. That key is what Storage::url() returns
            // for a *public* object, and these are not public: a recording is
            // reached only through a signed URL that MemoController::audio mints
            // after checking the owner. Setting it would hand out a permanent
            // unauthenticated link to somebody's voice.
            'use_path_style_endpoint' => env('AUDIO_BUCKET_PATH_STYLE', false),

            // **Both flipped from the framework's defaults, and both matter.**
            //
            // `throw`: with it false, Flysystem answers a failed write with
            // `false` rather than raising. MEMO-11's rule is that the blob must
            // be complete before the row referencing it is inserted -- a silent
            // false would insert a memo pointing at an object that was never
            // written, and the worker would fail on it minutes later with
            // nothing to say why. S3AudioStorage checks the return value too, so
            // the two cannot disagree about whether a write happened.
            'throw' => true,

            // `report`: a storage failure is already surfaced -- as a
            // StorageException, which is a 500 with a line in the log. Reporting
            // it as well would log every one of them twice.
            'report' => false,

            // Private, which is also R2's and S3's default for a new bucket, and
            // restated here because it is the property the whole scheme rests
            // on. A recording that were public-read would be readable by memo id
            // alone, which is precisely the hole the owner cookie exists to
            // close.
            'visibility' => 'private',
        ],

    ],

    /*
    |--------------------------------------------------------------------------
    | Symbolic Links
    |--------------------------------------------------------------------------
    |
    | Here you may configure the symbolic links that will be created when the
    | `storage:link` Artisan command is executed. The array keys should be
    | the locations of the links and the values should be their targets.
    |
    */

    'links' => [
        public_path('storage') => storage_path('app/public'),
    ],

];

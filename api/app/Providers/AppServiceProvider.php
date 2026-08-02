<?php

declare(strict_types=1);

namespace App\Providers;

use App\Contracts\AskBackend;
use App\Contracts\AudioStorage;
use App\Services\Ask\HttpAskBackend;
use App\Services\Health\HealthService;
use App\Services\Owners\OwnerContext;
use App\Storage\LocalAudioStorage;
use App\Storage\S3AudioStorage;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    /**
     * The only bindings this application needs. Controllers, services and
     * repositories are resolved from their constructor types, so adding a route
     * means adding a class and nothing else.
     */
    public function register(): void
    {
        // One instance per request, shared by the middleware that fills it and every
        // repository that reads it. `singleton` and not `scoped`, and the difference is
        // worth stating because it looks like the wrong choice: `scoped` exists for
        // long-running workers that serve many requests from one container, and this
        // application does not run one -- api/Dockerfile starts FrankenPHP without a worker
        // script, so the container is rebuilt per request and the two are identical here.
        // If a worker mode is ever enabled, this line must become `scoped` or the first
        // request's owner would be handed to every request after it. That is the single
        // most dangerous line in this file for that reason.
        $this->app->singleton(OwnerContext::class);

        // The swap point named in MEMO-05, now with something to swap to.
        //
        // Chosen by whether a bucket is configured rather than by a driver name, because the
        // two are not independent: `AUDIO_DRIVER=s3` with no bucket is a deployment that
        // boots happily and fails on the first upload, and every name that could be set is
        // one more way to spell a mistake. A bucket name is the thing that has to be true.
        //
        // The consequence is that local compose needs no new variable at all -- no
        // AUDIO_BUCKET, local driver, exactly as before -- and a hosted deployment turns this
        // on by configuring the storage it was always going to have to configure.
        $this->app->singleton(AudioStorage::class, function (): AudioStorage {
            if ((string) config('filesystems.disks.audio.bucket') === '') {
                return new LocalAudioStorage((string) config('memo.audio_dir'));
            }

            // The disk rather than the whole Storage manager, so S3AudioStorage depends on
            // the one thing it uses. `audio` and not `s3`: the default s3 disk is the
            // framework's and carries Laravel's own defaults, while this one is configured
            // for a recording -- private visibility, throwing on failure.
            return new S3AudioStorage(Storage::disk('audio'));
        });

        // The second swap point (MEMO-24): a hosted model, or an in-process one,
        // replaces this line and nothing else. `bind` rather than `singleton`
        // because there is no state and nothing expensive to build -- the client
        // is Laravel's, resolved per call.
        $this->app->bind(
            AskBackend::class,
            fn (): AskBackend => new HttpAskBackend(
                baseUrl: rtrim((string) config('memo.ask.url'), '/'),
                connectTimeout: (int) config('memo.ask.connect_timeout'),
                readTimeout: (int) config('memo.ask.read_timeout'),
            ),
        );

        // Contextual binding rather than reading config() inside the service, so the
        // service depends on the one value it uses instead of on the whole config
        // tree -- which is also what lets a test construct it with a chosen limit.
        $this->app->when(HealthService::class)
            ->needs('$maxAudioBytes')
            ->give(fn (): int => (int) config('memo.max_audio_bytes'));
    }

    public function boot(): void
    {
        //
    }
}

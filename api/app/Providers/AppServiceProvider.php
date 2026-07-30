<?php

declare(strict_types=1);

namespace App\Providers;

use App\Contracts\AudioStorage;
use App\Services\Health\HealthService;
use App\Storage\LocalAudioStorage;
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
        // The swap point named in MEMO-05: an S3 driver replaces this one line.
        $this->app->singleton(
            AudioStorage::class,
            fn (): AudioStorage => new LocalAudioStorage((string) config('memo.audio_dir')),
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

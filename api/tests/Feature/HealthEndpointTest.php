<?php

declare(strict_types=1);

namespace Tests\Feature;

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Tests\TestCase;

/**
 * Three tests for the three decisions MEMO-05 actually makes. Not coverage
 * theatre -- the repository and upload-validation suites belong to MEMO-25.
 */
final class HealthEndpointTest extends TestCase
{
    public function test_health_returns_200_and_reports_database_connectivity(): void
    {
        // The MEMO-05 acceptance criterion, verbatim.
        $this->getJson('/api/health')
            ->assertOk()
            ->assertJsonPath('status', 'ok')
            ->assertJsonPath('database.connected', true)
            ->assertJsonStructure([
                'status',
                'database' => ['connected', 'server_version', 'latency_ms'],
                'uploads' => ['max_audio_bytes', 'upload_max_filesize', 'post_max_size', 'accepts_max_audio'],
            ]);
    }

    public function test_unreachable_database_answers_503_without_leaking_the_dsn(): void
    {
        // The reason this is 503 rather than a 200 carrying "degraded": the compose
        // healthcheck is `curl -fsS`, which only fails on a non-2xx, and `web`
        // starts on `api: service_healthy`.
        config([
            'database.default' => 'pgsql',
            'database.connections.pgsql.url' => null,
            'database.connections.pgsql.host' => 'nosuchhost.invalid',
        ]);
        DB::purge('pgsql');

        // The service logs the driver message, which quotes host, port, role and
        // database. Swallowed here so a deliberately broken connection does not
        // look like a failing test.
        Log::spy();

        $response = $this->getJson('/api/health');

        $response->assertStatus(503)
            ->assertJsonPath('status', 'degraded')
            ->assertJsonPath('database.connected', false);

        $error = (string) $response->json('database.error');
        $this->assertStringContainsString('SQLSTATE', $error);
        $this->assertStringNotContainsString('nosuchhost.invalid', $error);
        $this->assertStringNotContainsString('password', $error);

        // Asserted, not merely silenced. The response deliberately withholds the
        // driver message, so the log is the only place that detail survives -- a
        // spy with no expectation would let a lost log entry pass as a success and
        // leave an unreachable database with no explanation anywhere.
        Log::shouldHaveReceived('error')
            ->once()
            ->withArgs(fn (string $message): bool => str_contains($message, 'Database health probe failed'));
    }

    public function test_an_unrouted_path_answers_json_even_without_an_accept_header(): void
    {
        // get(), not getJson(): no Accept: application/json. Laravel would
        // content-negotiate its way to an HTML error page here, and the frontend
        // parses every response as JSON -- so that surfaces in the browser as an
        // unexplained parse error instead of a 404. bootstrap/app.php forces JSON
        // unconditionally, and this is the test that says so.
        $response = $this->get('/api/no-such-route');

        $response->assertNotFound();
        $this->assertStringContainsString(
            'application/json',
            (string) $response->headers->get('Content-Type'),
        );
    }
}

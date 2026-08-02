<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Exceptions\AskUnavailable;
use App\Services\Ask\HttpAskBackend;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * The real AskBackend, against a faked HTTP layer.
 *
 * AskEndpointTest substitutes this class away, which is right for testing a route and leaves
 * the class itself uncovered -- and it is the one with the subtle behaviour: what is eager and
 * what is lazy, which upstream status becomes which sentence, and whether the body is read at
 * all on a refusal. None of that needs a container running a 1.5 GB model, only a fake
 * response, so none of it has an excuse to be untested.
 *
 * What `Http::fake()` cannot show is the part that was actually broken in this class before it
 * was measured: PHP's stream layer buffering a proxied socket until it has 8,192 bytes. A
 * faked body is a `php://temp` resource that is already complete, so it hands everything over
 * at once whatever the chunk size is. That property was verified against the running ai-api
 * instead, and the numbers are in the class's own comment.
 */
final class HttpAskBackendTest extends TestCase
{
    private const URL = 'http://ai-api:8000';

    private function backend(): HttpAskBackend
    {
        return new HttpAskBackend(self::URL, connectTimeout: 5, readTimeout: 210);
    }

    public function test_the_question_is_posted_as_json_to_the_ask_path(): void
    {
        Http::fake([self::URL.'/ask' => Http::response("{\"type\":\"done\"}\n", 200)]);

        iterator_to_array($this->backend()->ask('what about the dentist'));

        Http::assertSent(
            fn ($request): bool => $request->url() === self::URL.'/ask'
                && $request->method() === 'POST'
                && $request['question'] === 'what about the dentist',
        );
    }

    public function test_the_body_is_handed_back_unchanged(): void
    {
        $answer = '{"type":"sources","sources":[]}'."\n".'{"type":"done","cited":[]}'."\n";

        Http::fake([self::URL.'/ask' => Http::response($answer, 200)]);

        $chunks = iterator_to_array($this->backend()->ask('anything at all'));

        $this->assertSame($answer, implode('', $chunks));
    }

    /**
     * **The property this class is most easily broken by, and it was broken by it once.**
     *
     * A method containing a `yield` anywhere is a generator function: none of its body runs
     * until the first iteration, which is after the controller has returned 200 and Symfony
     * has begun sending it. So everything that decides a status code has to happen in a method
     * with no `yield` in it. Asserting that the request was sent *before* anything is iterated
     * is what pins that.
     */
    public function test_the_request_is_made_before_the_result_is_iterated(): void
    {
        Http::fake([self::URL.'/ask' => Http::response("{\"type\":\"done\"}\n", 200)]);

        $this->backend()->ask('what about the dentist');

        Http::assertSentCount(1);
    }

    public function test_a_503_carries_the_reason_ai_api_gave(): void
    {
        // ai-api knows which of missing, loading or failed it is in; this side knows only
        // "503". Reported as "still loading", a missing model sends somebody off to wait for
        // something that will never happen.
        Http::fake([
            self::URL.'/ask' => Http::response(
                ['model' => 'missing', 'message' => 'The local model is not in this image.'],
                503,
            ),
        ]);

        $this->expectException(AskUnavailable::class);
        $this->expectExceptionMessage('The local model is not in this image.');

        $this->backend()->ask('what about the dentist');
    }

    public function test_a_503_with_nothing_useful_in_it_falls_back_to_our_own_sentence(): void
    {
        Http::fake([self::URL.'/ask' => Http::response('not json at all', 503)]);

        $this->expectException(AskUnavailable::class);
        $this->expectExceptionMessage('still loading its model');

        $this->backend()->ask('what about the dentist');
    }

    public function test_any_other_status_names_itself(): void
    {
        Http::fake([self::URL.'/ask' => Http::response('', 500)]);

        $this->expectException(AskUnavailable::class);
        $this->expectExceptionMessage('answered 500');

        $this->backend()->ask('what about the dentist');
    }

    public function test_a_connection_failure_names_the_container_rather_than_the_host(): void
    {
        Http::fake(fn () => throw new ConnectionException('cURL error 6: Could not resolve host'));

        try {
            $this->backend()->ask('what about the dentist');

            $this->fail('Expected AskUnavailable.');
        } catch (AskUnavailable $e) {
            $this->assertStringContainsString('docker compose ps ai-api', $e->getMessage());

            // The sentence reaches `curl -i` and the api log, so App\Exceptions\StorageException's
            // rule applies: nothing about the internals, and no host for somebody to go and
            // ping. The cURL text is on the previous exception if anybody wants it.
            $this->assertStringNotContainsString('cURL', $e->getMessage());
            $this->assertStringNotContainsString('ai-api:8000', $e->getMessage());
        }
    }
}

<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Repositories\MemoRepository;
use Illuminate\Testing\TestResponse;
use Tests\Support\FakeMemoRepository;
use Tests\TestCase;

/**
 * App\Http\Middleware\ValidateJsonBody, and the line it draws between a body that is
 * broken and a body that is absent.
 *
 * Its own file rather than an addition to HealthEndpointTest: that one is scoped by
 * its docblock to the three decisions MEMO-05 makes, and this is neither a health
 * check nor an endpoint's own contract.
 */
final class JsonRequestBodyTest extends TestCase
{
    public function test_a_truncated_json_body_blames_the_body_and_not_a_missing_field(): void
    {
        $response = $this->postRaw('/api/memos', '{"text": ');

        $response->assertStatus(400);

        $message = (string) $response->json('message');

        $this->assertStringContainsString('not valid JSON', $message);
        // The regression this exists for: the same request used to answer 422 "The
        // text field is required.", sending the caller to look at a field that was
        // there. Asserted as an absence, because a 400 that still said "required"
        // would pass every other assertion here.
        $this->assertStringNotContainsString('required', $message);
    }

    public function test_the_decoders_own_reason_reaches_the_caller(): void
    {
        // HttpException messages survive APP_DEBUG=false, and this one describes the
        // caller's body rather than anything of ours, so it is safe to pass on and
        // saves them guessing which part of the document is wrong.
        //
        // Two bodies with two different reasons, because one alone would pass just as
        // well if the message were a constant with the word "error" in it.
        $this->postRaw('/api/memos', '{"text": ')
            ->assertStatus(400)
            ->assertJsonPath('message', 'The request body is not valid JSON: Syntax error.');

        $this->postRaw('/api/memos', '{"text": "unterminated}')
            ->assertStatus(400)
            ->assertJsonPath(
                'message',
                'The request body is not valid JSON: Control character error, possibly incorrectly encoded.',
            );
    }

    public function test_valid_json_that_is_not_an_object_is_not_this_middlewares_problem(): void
    {
        // A bare scalar is well-formed JSON, so it passes here and lands in the input
        // bag as [5] -- no field named text, and 422 "the text field is required" is a
        // fair description of that. Recorded as a boundary rather than an oversight:
        // this middleware answers "could the body be read", and what a readable body
        // is allowed to contain belongs to the route.
        $this->postRaw('/api/memos', '5')
            ->assertStatus(422)
            ->assertJsonValidationErrors('text');
    }

    public function test_a_body_that_is_not_json_at_all_is_rejected(): void
    {
        $this->postRaw('/api/memos', '<html><body>nope</body></html>')->assertStatus(400);
    }

    public function test_the_rejection_is_itself_json(): void
    {
        // The whole reason bootstrap/app.php forces JSON unconditionally: the frontend
        // parses every response as JSON, so an HTML 400 would surface in the browser
        // as an unexplained parse error rather than as a bad request.
        $response = $this->postRaw('/api/memos', '{"text": ');

        $this->assertStringContainsString(
            'application/json',
            (string) $response->headers->get('Content-Type'),
        );
    }

    public function test_an_empty_body_is_absence_and_gets_the_routes_own_answer(): void
    {
        // Deliberately not a 400. The caller sent nothing, so "the text field is
        // required" is both true and the useful thing to say. It also keeps this
        // middleware agreeing with Request::json(), which substitutes '[]' when the
        // body trims to empty.
        $this->postRaw('/api/memos', '')
            ->assertStatus(422)
            ->assertJsonValidationErrors('text');

        $this->postRaw('/api/memos', "  \n\t ")
            ->assertStatus(422)
            ->assertJsonValidationErrors('text');
    }

    public function test_a_body_declared_as_something_other_than_json_is_not_judged_here(): void
    {
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        // Form-encoded, carrying a real field, and asserted as a 201 rather than as
        // some 4xx: a body that is not JSON must reach the route and work, not merely
        // avoid being called malformed. That is the case MEMO-11 depends on -- its
        // multipart audio upload arrives declared as something other than JSON, and
        // this middleware holds callers to the format they declared instead of
        // sniffing bodies.
        $this->post('/api/memos', ['text' => 'from a form'], ['Accept' => 'application/json'])
            ->assertCreated()
            ->assertJsonPath('memo.transcript', 'from a form');
    }

    public function test_a_well_formed_body_still_reaches_the_route(): void
    {
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $this->postRaw('/api/memos', '{"text":"still works"}')
            ->assertCreated()
            ->assertJsonPath('memo.transcript', 'still works');
    }

    public function test_a_request_that_carries_no_body_is_unaffected(): void
    {
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $this->getJson('/api/health')->assertOk();
        $this->getJson('/api/memos?limit=1')->assertOk();
    }

    public function test_a_malformed_body_answers_400_even_where_no_route_matches(): void
    {
        // The consequence of registering this globally rather than on the api group:
        // it runs before routing, so this is a 400 and not a 404. Pinned so that the
        // ordering stays a decision. An unreadable request is unreadable whether or
        // not a route wanted it, which is the same order ValidatePostSize applies.
        $this->postRaw('/api/no-such-route', '{"text": ')->assertStatus(400);
    }

    /** A raw body, which post()/postJson() cannot send -- both serialise an array. */
    private function postRaw(string $uri, string $body): TestResponse
    {
        return $this->call('POST', $uri, [], [], [], [
            'CONTENT_TYPE' => 'application/json',
            'HTTP_ACCEPT' => 'application/json',
        ], $body);
    }
}

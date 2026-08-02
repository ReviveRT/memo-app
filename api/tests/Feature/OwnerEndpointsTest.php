<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Repositories\MemoRepository;
use App\Support\OwnerToken;
use Symfony\Component\HttpFoundation\Cookie;
use Tests\Support\FakeMemoRepository;
use Tests\TestCase;

/**
 * Owner resolution, the claim routes, and the cookie's attributes.
 *
 * **Every assertion about the cookie here is a security assertion**, which is why they are
 * spelled out one flag at a time rather than compared against a whole Set-Cookie string. A
 * missing `HttpOnly` or a `SameSite` that relaxed to `None` would not fail any other test in
 * this project, would not show up as a broken request, and would not be visible in the UI --
 * the application would keep working exactly as it does now while being open to a
 * cross-site POST or to having its token read by injected script. There is nothing else in
 * the suite that would catch either.
 *
 * What these tests cannot show is the part that matters most: that a token is unguessable.
 * That is a property of random_bytes and of the 128-bit width, argued in App\Support\
 * OwnerToken, and no test can demonstrate it -- asserting that two mints differ would pass
 * for a counter.
 */
final class OwnerEndpointsTest extends TestCase
{
    private const OWNER_ID = '01900000-0000-7000-8000-0000000000aa';

    private function cookieFrom($response): ?Cookie
    {
        foreach ($response->headers->getCookies() as $cookie) {
            if ($cookie->getName() === (string) config('memo.owner.cookie')) {
                return $cookie;
            }
        }

        return null;
    }

    public function test_the_bootstrap_route_mints_an_owner_and_sets_the_cookie(): void
    {
        $response = $this->getJson('/api/owner');

        $response->assertOk()->assertJsonStructure(['owner' => ['id']]);

        $this->assertCount(1, $this->owners->inserted, 'exactly one owner should be created');

        $cookie = $this->cookieFrom($response);

        $this->assertNotNull($cookie, 'the bootstrap must hand the browser its identity');
        $this->assertSame($this->owners->inserted[0]['token_hash'], OwnerToken::hash($cookie->getValue()));
    }

    public function test_the_cookie_is_http_only_and_same_site_lax(): void
    {
        $cookie = $this->cookieFrom($this->getJson('/api/owner'));

        // HttpOnly: script on this origin can read the memos anyway, but it must not be able
        // to read the token and keep it after the tab is gone.
        $this->assertTrue($cookie->isHttpOnly(), 'the token must not be readable by script');

        // Lax is the whole CSRF defence -- there is no VerifyCsrfToken in this application,
        // because bootstrap/app.php registers no `web` middleware group. If this ever reads
        // 'none', a cross-site form post can delete somebody's memos.
        $this->assertSame(Cookie::SAMESITE_LAX, $cookie->getSameSite());

        $this->assertSame('/', $cookie->getPath());
    }

    public function test_the_cookie_is_marked_secure_only_when_the_request_was(): void
    {
        // Plain http, as local compose serves it. A hardcoded Secure here would mean the
        // cookie is silently never stored in development.
        $this->assertFalse($this->cookieFrom($this->getJson('http://localhost/api/owner'))->isSecure());

        // https, as every hosting platform serves it once TrustProxies reads
        // X-Forwarded-Proto. Without this the bearer token travels in clear text.
        $this->assertTrue($this->cookieFrom($this->getJson('https://localhost/api/owner'))->isSecure());
    }

    public function test_a_cookieless_read_answers_empty_without_creating_an_owner(): void
    {
        // The free-tier decision in ResolveOwner: an uptime pinger, a crawler or a link
        // previewer must not leave a row behind. At one ping a minute an eager mint is 1,440
        // owners a day against a quota measured in hundreds of megabytes.
        $repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $repository);

        $response = $this->getJson('/api/memos');

        $response->assertOk();

        $this->assertSame([], $this->owners->inserted, 'a bot must not create an owner');
        $this->assertNull($this->cookieFrom($response), 'and must not be handed an identity');
    }

    public function test_a_write_without_a_cookie_does_mint_one(): void
    {
        // The other half of the rule: a write has a foreign key to satisfy, so it needs a
        // real row. Without this the INSERT would fail against the nil transient owner.
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $response = $this->postJson('/api/memos', ['text' => 'Call the dentist']);

        $response->assertCreated();

        $this->assertCount(1, $this->owners->inserted);
        $this->assertNotNull($this->cookieFrom($response));
    }

    public function test_a_known_cookie_resolves_rather_than_minting_again(): void
    {
        // A write, not a read, and deliberately. A GET would prove nothing here: a cookie
        // that failed to resolve takes the transient path, which also answers 200 and also
        // inserts nothing -- so the obvious version of this test passes whether or not the
        // cookie was understood. Only a write distinguishes them, because only a write mints.
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $response = $this->actingAsOwner(self::OWNER_ID)
            ->postJson('/api/memos', ['text' => 'Call the dentist']);

        $response->assertCreated();

        $this->assertSame([], $this->owners->inserted, 'a returning browser is not a new owner');
        $this->assertNull($this->cookieFrom($response), 'and needs no fresh cookie');
    }

    public function test_an_unknown_or_malformed_cookie_is_treated_as_a_first_visit(): void
    {
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $name = (string) config('memo.owner.cookie');

        // A pruned owner, a database that was reset, and a guess are the same thing from
        // here -- and none of them is an error worth showing somebody.
        foreach (['zzzzzzzzzzzzzzzzzzzzzz', 'not-a-token', ''] as $value) {
            $this->withCredentials()
                ->withUnencryptedCookies([$name => $value])
                ->getJson('/api/memos')
                ->assertOk();
        }

        $this->assertSame([], $this->owners->inserted, 'still a safe read, still no row');
    }

    public function test_claiming_a_valid_link_adopts_that_owner_in_this_browser(): void
    {
        $token = 'bbbbbbbbbbbbbbbbbbbbbb';
        $this->owners->give($token, self::OWNER_ID);

        $response = $this->get('/api/claim/'.$token);

        $response->assertRedirect('/memos?claim=ok');

        $this->assertSame($token, $this->cookieFrom($response)->getValue());
    }

    public function test_claiming_wins_over_a_stale_cookie_the_middleware_would_have_refreshed(): void
    {
        // **The ordering bug this pins.** ResolveOwner writes its cookie *after* the
        // controller has run, because the refresh decision is made before `$next()` and
        // applied to the response that comes back. So a browser whose own cookie happens to
        // be due its once-a-day refresh follows a claim link, the controller sets the new
        // identity, and the middleware then overwrites it with the old one -- the claim
        // silently does nothing, and the only way to notice is that the memos are still the
        // wrong ones.
        //
        // Reachable by anybody who has used the app before and not in the last day, which is
        // most people following a claim link: the whole point of the link is arriving from
        // somewhere else. The existing claim test misses it because a request with no cookie
        // takes the transient path and never refreshes.
        $mine = 'eeeeeeeeeeeeeeeeeeeeee';
        $theirs = 'ffffffffffffffffffffff';

        // Old enough that ResolveOwner's staleness check fires for the presented cookie.
        $this->owners->lastSeenAt = '2020-01-01T00:00:00.000Z';
        $this->owners->give($mine, self::OWNER_ID);
        $this->owners->give($theirs, '01900000-0000-7000-8000-0000000000bb');

        $response = $this->withCredentials()
            ->withUnencryptedCookies([(string) config('memo.owner.cookie') => $mine])
            ->get('/api/claim/'.$theirs);

        $response->assertRedirect('/memos?claim=ok');

        $this->assertSame(
            $theirs,
            $this->cookieFrom($response)->getValue(),
            'the claimed identity must survive the middleware refresh',
        );
    }

    public function test_a_malformed_or_unknown_claim_redirects_rather_than_showing_json(): void
    {
        // The person is looking at a browser window, not a status code: a 404 document here
        // would be Laravel's error page, and a JSON body would be a page of JSON.
        $this->get('/api/claim/not-a-token')->assertRedirect('/memos?claim=invalid');
        $this->get('/api/claim/cccccccccccccccccccccc')->assertRedirect('/memos?claim=unknown');
    }

    public function test_the_claim_link_is_only_handed_to_a_browser_that_already_has_the_token(): void
    {
        $token = 'dddddddddddddddddddddd';
        $this->owners->give($token, self::OWNER_ID);

        $response = $this->withCredentials()
            ->withUnencryptedCookies([(string) config('memo.owner.cookie') => $token])
            ->postJson('/api/owner/claim-link');

        $response->assertOk()->assertJsonPath('claim_url', 'http://localhost/api/claim/'.$token);
    }

    public function test_asking_for_a_memo_belonging_to_nobody_here_is_a_404_and_not_a_403(): void
    {
        // The scoping and the not-found path are the same code -- see MemoRepository::ownerId.
        // So a probe cannot tell a taken memo id from a free one, and there is no second
        // answer to leak through. The fake returns no rows, which is what a foreign memo
        // looks like from a scoped query.
        $this->app->instance(MemoRepository::class, new FakeMemoRepository);

        $this->actingAsOwner(self::OWNER_ID)
            ->patchJson('/api/memos/01900000-0000-7000-8000-0000000000f1', ['title' => 'Mine now'])
            ->assertNotFound();
    }
}

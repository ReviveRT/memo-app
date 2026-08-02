<?php

namespace Tests;

use App\Repositories\OwnerRepository;
use Illuminate\Foundation\Testing\TestCase as BaseTestCase;
use Tests\Support\FakeOwnerRepository;

/**
 * The base every test extends.
 *
 * It was empty until owners arrived. What it does now, it does for one reason:
 * App\Http\Middleware\ResolveOwner is *global* middleware, so it runs before every route in
 * the suite, and its first act is a query the real OwnerRepository writes for Postgres --
 * to_char, RETURNING -- against a suite that runs on sqlite in memory (phpunit.xml). Without
 * a stand-in bound here, every feature test in the project would fail on a query none of
 * them are about.
 *
 * Binding it once here rather than in eleven setUp() methods is not only less repetition: a
 * test file added later inherits owner resolution without having to know that owners exist,
 * which is the same property being global middleware gives the application.
 */
abstract class TestCase extends BaseTestCase
{
    /**
     * The owner repository every test runs against. Available to a test that cares -- about
     * minting, about the claim routes -- so it can seed a token or read back what was written.
     */
    protected FakeOwnerRepository $owners;

    protected function setUp(): void
    {
        parent::setUp();

        $this->owners = new FakeOwnerRepository;
        $this->app->instance(OwnerRepository::class, $this->owners);
    }

    /**
     * Make the next request arrive as an owner who already exists.
     *
     * **Two Laravel defaults conspire here, and both fail silently.** Each was hit while
     * writing OwnerEndpointsTest, and neither produced an error -- the requests kept
     * answering 200 as a brand new owner, so the tests passed while proving nothing.
     *
     *   * `withCookies` *encrypts* what it is given, expecting EncryptCookies to decrypt it
     *     on the way in. This application has no such middleware: bootstrap/app.php registers
     *     no `web` group at all. The encrypted blob matches no token, so ResolveOwner treats
     *     the request as a first visit. `withUnencryptedCookies` is the one that applies.
     *   * `getJson` and `postJson` send **no cookies whatsoever** unless `withCredentials()`
     *     was called first -- `prepareCookiesForJsonRequest` returns an empty array
     *     otherwise. That mirrors what XHR does without `credentials`, which is reasonable of
     *     it and lethal here, because every route in this application is a JSON route and the
     *     identity it runs on is a cookie.
     *
     * Wrapping both here rather than leaving each test to remember is the point: the helper
     * hands back the pending request, so there is no array left over for a caller to pass to
     * the wrong method.
     */
    protected function actingAsOwner(string $ownerId, string $token = 'aaaaaaaaaaaaaaaaaaaaaa'): static
    {
        $this->owners->give($token, $ownerId);

        return $this->withCredentials()
            ->withUnencryptedCookies([(string) config('memo.owner.cookie') => $token]);
    }
}

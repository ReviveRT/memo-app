<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use App\Repositories\OwnerRepository;
use App\Services\Owners\Owner;
use App\Services\Owners\OwnerContext;
use App\Support\OwnerCookie;
use App\Support\OwnerToken;
use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Str;
use Symfony\Component\HttpFoundation\Response;

/**
 * Turns a cookie into the owner every query below is scoped by, minting one if this
 * browser has never been here.
 *
 * There is no login, so this runs on every API request and always succeeds -- there is no
 * 401 anywhere in this application and no state in which a person is "logged out". What
 * varies is only whether the owner it resolves is one the database has seen before.
 *
 * Every attribute of the cookie itself is decided by App\Support\OwnerCookie, including
 * the SameSite setting that is this application's only CSRF defence. It is shared with the
 * claim route rather than spelled out twice.
 *
 * **Minting is lazy, and this is a free-tier decision rather than an optimisation.** An
 * owner row is created only for a request that needs one to exist. A safe read arriving
 * with no cookie gets a transient owner instead: it matches no rows, answers an empty
 * list, and writes nothing. The alternative -- mint for anybody who knocks -- means every
 * crawler, link previewer and uptime pinger creates a permanent row, and a free deployment
 * has an uptime pinger by construction, because that is how you stop the instance
 * sleeping. At one ping a minute that is 1,440 owners a day against a quota measured in
 * hundreds of megabytes. The `memo:prune-owners` command would eventually collect them; not
 * creating them is better.
 */
final class ResolveOwner
{
    /**
     * The owner id given to a request that has no cookie and does not need one.
     *
     * The nil UUID, chosen because it is the one uuid this application can never mint --
     * every real id is a v7 from Str::uuid7() or a v4 from the migration's backfill. So it
     * matches no memo, no collection and no reminder, which is exactly the answer a browser
     * with no identity should get.
     *
     * It is also not present in `owners`, which makes the failure mode right if this ever
     * reaches a write: the foreign keys 007_owners.sql declares reject the INSERT and the
     * request 500s. That is a bug being caught rather than a case to handle -- needsOwner()
     * below is what guarantees a write never sees this.
     */
    private const TRANSIENT_OWNER_ID = '00000000-0000-0000-0000-000000000000';

    /**
     * How stale last_seen_at may get before a read refreshes it, in seconds.
     *
     * A day. The column exists to answer "has anybody used this account in a year", which
     * one day of resolution answers perfectly well, and the cost of finer resolution is
     * paid on every single request: writing it eagerly turns every GET into a read-write
     * transaction and makes this table's write volume equal to the application's total
     * traffic. This is also what bounds how often a response carries a refreshed cookie.
     */
    private const SEEN_RESOLUTION = 86_400;

    public function __construct(
        private readonly OwnerRepository $owners,
        private readonly OwnerContext $context,
    ) {}

    public function handle(Request $request, Closure $next): Response
    {
        $presented = $request->cookies->get(OwnerCookie::name());

        $owner = null;
        $token = null;

        // A cookie that is not a string, not the right shape, or not in the table all land
        // here as "no owner". They are not told apart deliberately: a browser cannot act on
        // the difference between a token that was pruned and one that was never valid, and
        // the only useful response to all three is to behave like a first visit.
        if (is_string($presented) && OwnerToken::isWellFormed($presented)) {
            $found = $this->owners->findByTokenHash(OwnerToken::hash($presented));

            if ($found !== null) {
                $owner = $found;
                $token = $presented;
            }
        }

        $minted = false;

        if ($owner === null) {
            if ($this->needsOwner($request)) {
                $token = OwnerToken::mint();
                $owner = $this->owners->insert(Str::uuid7()->toString(), OwnerToken::hash($token));
                $minted = true;
            } else {
                // Transient: bound so repositories have something to scope by, never
                // persisted, and never handed a cookie.
                $owner = new Owner(self::TRANSIENT_OWNER_ID, '1970-01-01T00:00:00.000Z');
                $token = '';
            }
        }

        $this->context->set($owner, $token);

        $stale = $token !== '' && ! $minted && $this->lastSeenIsStale($owner);

        if ($stale) {
            $this->owners->touch($owner->id, $this->cutoff());
        }

        $response = $next($request);

        // The cookie goes out when it is new, and again whenever the once-a-day touch fires.
        // The second case is what makes the lifetime a sliding window rather than a hard
        // deadline: somebody who uses this weekly never loses their memos to expiry, while a
        // response still carries Set-Cookie at most once a day rather than on every request.
        //
        // **Never over the top of a cookie the handler set itself.** This runs after
        // `$next()`, because the response is what a cookie is attached to -- so without the
        // guard the refresh silently overwrites OwnerController::claim's work whenever the
        // browser following a claim link happened to be due its daily refresh. The claim then
        // does nothing at all, and the only symptom is that the memos are still the wrong
        // ones. It is not an edge case either: arriving with an old cookie is the normal way
        // to follow a claim link, since the point of the link is coming from somewhere else.
        //
        // Expressed as "the handler wins" rather than as "skip this on the claim route",
        // which was the other fix and is worse: it names one route in a middleware that knows
        // nothing about routes, and it would have to be remembered by whoever adds the second
        // handler that sets this cookie.
        if (($minted || $stale) && ! $this->alreadySet($response)) {
            $response->headers->setCookie(OwnerCookie::for($token, $request));
        }

        return $response;
    }

    /**
     * Whether the response already carries an owner cookie, put there by whatever handled the
     * request.
     *
     * Matched on the name alone. Path and domain are not compared even though setCookie()
     * keys on all three, and that is deliberate: a handler setting this cookie on a different
     * path is doing something this middleware should stay out of either way, and comparing
     * would make the guard miss it.
     */
    private function alreadySet(Response $response): bool
    {
        foreach ($response->headers->getCookies() as $cookie) {
            if ($cookie->getName() === OwnerCookie::name()) {
                return true;
            }
        }

        return false;
    }

    /**
     * Whether this request must have a real, persisted owner behind it.
     *
     * Safe methods do not: a GET can be answered from an empty result set, and answering it
     * that way is what keeps bots out of the owners table. Everything else does, because it
     * is going to write a row whose foreign key has to resolve.
     *
     * `GET /api/owner` is the deliberate exception, and it is the whole bootstrap protocol:
     * it is the one read whose *purpose* is to establish an identity, so the frontend calls
     * it once before anything else and every later request arrives with a cookie. That
     * ordering is also what keeps a cold load from minting several owners at once -- three
     * parallel cookie-less requests would each mint one, and the browser would keep whichever
     * Set-Cookie landed last.
     */
    private function needsOwner(Request $request): bool
    {
        if (! $request->isMethodSafe()) {
            return true;
        }

        return $request->is('api/owner');
    }

    private function lastSeenIsStale(Owner $owner): bool
    {
        return strtotime($owner->lastSeenAt) < time() - self::SEEN_RESOLUTION;
    }

    /**
     * The instant last_seen_at must predate for a touch to be worth doing, as an ISO string
     * the database will compare against a timestamptz.
     */
    private function cutoff(): string
    {
        return gmdate('Y-m-d\TH:i:s\Z', time() - self::SEEN_RESOLUTION);
    }

}

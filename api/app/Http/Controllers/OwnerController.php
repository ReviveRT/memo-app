<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Repositories\OwnerRepository;
use App\Services\Owners\OwnerContext;
use App\Support\OwnerCookie;
use App\Support\OwnerToken;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\RedirectResponse;

/**
 * The three routes that exist because this application has owners but no accounts.
 *
 * None of them authenticate anything. `show` establishes an identity, `claimLink` hands
 * the secret for one back to whoever already holds it, and `claim` moves it to another
 * browser. There is nothing to log in to and nothing to log out of.
 */
final class OwnerController extends Controller
{
    public function __construct(
        private readonly OwnerContext $context,
        private readonly OwnerRepository $owners,
    ) {}

    /**
     * The frontend's bootstrap call, and the only safe read that mints an owner.
     *
     * ResolveOwner::needsOwner names this path explicitly, so a browser arriving with no
     * cookie leaves this request with one. That is why the frontend awaits it before
     * fetching anything else: three parallel cookie-less requests would otherwise mint three
     * owners and keep whichever Set-Cookie arrived last, stranding memos written against the
     * other two.
     *
     * The response carries the owner id and not the token. The id is not a secret -- it
     * appears in no URL and grants nothing -- and having it lets the frontend tell "the same
     * person as last time" from "a new identity", which is what decides whether to show the
     * first-run explanation.
     */
    public function show(): JsonResponse
    {
        return response()->json(['owner' => ['id' => $this->context->current()->id]]);
    }

    /**
     * The shareable link that moves this owner's memos to another browser.
     *
     * **POST, and not because it changes anything -- it does not.** This is the one response
     * in the application that contains the bearer token, so it is deliberately not something
     * a page load produces. Behind a POST it is reached only when somebody presses the
     * button, which keeps the secret out of the memory of every tab that merely has the app
     * open, and out of any logging or proxy layer that records response bodies for reads. A
     * GET would have been the honest verb and the worse choice.
     *
     * Lax also means this cannot be triggered by a cross-site form post, which for a route
     * that emits a credential is the property that matters most.
     */
    public function claimLink(Request $request): JsonResponse
    {
        $token = $this->context->token();

        // A transient owner has no token and nothing to share. Reachable only by POSTing
        // here with no cookie, which ResolveOwner would have minted for -- so in practice
        // this is unreachable, and it is here so that a future change to needsOwner() cannot
        // turn it into an empty URL that silently claims nothing.
        if ($token === '') {
            return response()->json(
                ['message' => 'This browser has no memos to share yet. Record one first.'],
                409,
            );
        }

        return response()->json([
            'claim_url' => $request->getSchemeAndHttpHost().'/api/claim/'.$token,
        ]);
    }

    /**
     * Adopt an owner in this browser, then get out of the way.
     *
     * A redirect rather than JSON, because this URL is opened by a person in an address bar
     * rather than fetched by the frontend -- answering `{"owner":{...}}` would show them a
     * page of JSON. The redirect target is the SPA, which then bootstraps against the cookie
     * this response just set.
     *
     * `/memos` and not `/`, which is the landing page. Somebody following this link has
     * asked to see their memos, and `/` is a title and a button -- it would make them press
     * one more thing to find out whether the link had worked. The `?claim=` parameter is
     * read there and rendered as a notice.
     *
     * **The cookie set here replaces whatever the middleware decided.** ResolveOwner has
     * already run and may have refreshed the *previous* owner's cookie on its once-a-day
     * schedule. setCookie() replaces by name, path and domain, and all three match, so the
     * response carries one Set-Cookie and it is this one. Claiming is destructive in exactly
     * that sense and it is the intended meaning: a browser holds one identity, and adopting
     * a link means letting go of the one it had. The memos behind the old token are not
     * deleted and the old link still works -- whoever has it can claim back.
     *
     * A bad token redirects rather than 404s, for the same reason the success case does: the
     * person is looking at a browser window, not at a status code. The SPA reads the query
     * parameter and explains.
     */
    public function claim(Request $request, string $token): RedirectResponse
    {
        if (! OwnerToken::isWellFormed($token)) {
            return redirect('/memos?claim=invalid');
        }

        $owner = $this->owners->findByTokenHash(OwnerToken::hash($token));

        // Well-formed but unknown. Almost always a link whose owner was pruned for inactivity
        // or one from a database that has since been reset -- not an attack, since guessing a
        // valid 128-bit token is not a thing that happens. Same answer either way.
        if ($owner === null) {
            return redirect('/memos?claim=unknown');
        }

        $response = redirect('/memos?claim=ok');

        $response->headers->setCookie(OwnerCookie::for($token, $request));

        return $response;
    }
}

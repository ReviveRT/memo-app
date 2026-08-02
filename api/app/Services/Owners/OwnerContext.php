<?php

declare(strict_types=1);

namespace App\Services\Owners;

use RuntimeException;

/**
 * The owner of the request being handled, injected into every repository that touches an
 * owned table.
 *
 * **Why a mutable holder rather than injecting Owner itself.** An Owner cannot be
 * constructed until the cookie has been read, and repositories are constructed by the
 * container whenever something asks for one -- which for a `singleton` binding may be
 * before middleware has run at all. A holder resolves the ordering: the container wires
 * this object in at build time, ResolveOwner fills it in during the request, and the
 * repository reads it at query time, by which point it is always set.
 *
 * **Why not pass the owner as a parameter to every repository method.** That was the
 * other candidate and it is the more explicit design: a method that needs an owner says
 * so in its signature, and the compiler catches a caller who forgot. It was rejected on
 * the size of the change rather than on principle -- it puts an `$ownerId` argument
 * through three controllers, four services and roughly twenty repository methods, none of
 * which have any decision to make about it. What it buys is a guarantee, and the guarantee
 * is available more cheaply: see current() below.
 *
 * **Fail closed.** current() throws when no owner has been set, so a query that runs
 * outside a resolved request fails loudly instead of quietly reading across owners. That
 * is the property the parameter-passing design would have given, recovered at runtime
 * rather than at author time: the failure surfaces the first time the code path executes
 * instead of the first time it is compiled, which for a repository method with any test
 * coverage at all is the same moment. It also means adding a new query to a repository
 * cannot accidentally produce an unscoped one -- the worst outcome is a 500 rather than
 * one person reading another's memos.
 *
 * HealthRepository deliberately does not take this: it counts rows to prove the database
 * answers, which is a question about the deployment and not about anybody's memos.
 */
final class OwnerContext
{
    private ?Owner $owner = null;

    /**
     * The plaintext token this request arrived with, when it arrived with a valid one.
     *
     * Held apart from the Owner for the reason that class gives -- a bearer secret has no
     * business on a value object that gets passed around -- and held at all for exactly one
     * caller: OwnerController::claimLink, which has to echo the token back to build a
     * shareable URL. Nothing else may read it, and nothing else does.
     */
    private ?string $token = null;

    /**
     * The owner of the current request.
     *
     * @throws RuntimeException When no owner has been resolved, which means either that
     *                          ResolveOwner is not in the middleware stack for this route
     *                          or that this code is running outside an HTTP request (an
     *                          artisan command, say). Both are bugs rather than states to
     *                          handle, and both should stop the request rather than fall
     *                          back to some default owner -- there is no such thing here,
     *                          and inventing one would silently pool everybody's memos.
     */
    public function current(): Owner
    {
        return $this->owner ?? throw new RuntimeException(
            'No owner has been resolved for this request. '
            .'A query against an owned table ran outside the ResolveOwner middleware.'
        );
    }

    public function set(Owner $owner, string $token): void
    {
        $this->owner = $owner;
        $this->token = $token;
    }

    /**
     * The plaintext token, for building a claim URL and for nothing else.
     *
     * @throws RuntimeException When called with no owner resolved, matching current().
     */
    public function token(): string
    {
        return $this->token ?? throw new RuntimeException(
            'No owner token is available for this request.'
        );
    }
}

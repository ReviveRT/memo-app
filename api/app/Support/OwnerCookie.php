<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Cookie;

/**
 * The one place the owner cookie's attributes are decided.
 *
 * Two callers set this cookie -- ResolveOwner when it mints or refreshes, and
 * OwnerController::claim when a link is adopted -- and every attribute below is a security
 * property. A second copy that drifted by one flag would be a real vulnerability with no
 * visible symptom: a `Secure` that went missing ships the token in clear text, a `SameSite`
 * that relaxed to None removes the only CSRF defence this application has. Neither shows up
 * as a failing request.
 */
final class OwnerCookie
{
    private function __construct() {}

    public static function name(): string
    {
        return (string) config('memo.owner.cookie');
    }

    /**
     * @param  string  $token  The plaintext token. This is the only value the cookie ever
     *                         carries; there is no signing envelope and nothing else packed
     *                         alongside it.
     */
    public static function for(string $token, Request $request): Cookie
    {
        return Cookie::create(self::name())
            ->withValue($token)

            // Chrome clamps any cookie expiry beyond 400 days to 400 days, so a larger
            // number here would not mean what it said. config/memo.php defaults to exactly
            // that cap and explains it; ResolveOwner refreshes on use, which is what turns a
            // hard 400-day deadline into a sliding window for anybody who keeps using the
            // app.
            ->withExpires(time() + (int) config('memo.owner.lifetime_days') * 86_400)

            ->withPath('/')

            // HttpOnly, so an XSS cannot read the token out and keep it. A smaller win than
            // it looks -- script on this origin can already read every memo through the API
            // -- but the difference is durability: reading memos ends when the tab closes
            // and a stolen token does not.
            ->withHttpOnly(true)

            // **The CSRF defence, not a refinement of one.** bootstrap/app.php registers no
            // `web` middleware group, so there is no VerifyCsrfToken and no token anywhere
            // in this application. Lax withholds the cookie from every cross-site request
            // except a top-level GET navigation, which covers every write route here.
            // Loosening this to None -- which a split-origin deployment would require --
            // means bringing a CSRF token along with it.
            ->withSameSite(Cookie::SAMESITE_LAX)

            // Follows the request rather than being configured, so one image is correct both
            // behind a hosting proxy terminating TLS and on plain http in local compose. A
            // hardcoded true means the cookie is never stored in development; a hardcoded
            // false ships the token in clear text wherever https was available.
            //
            // isSecure() reads X-Forwarded-Proto only from a trusted proxy, which is why
            // bootstrap/app.php configures trustProxies. Without it this is false on every
            // hosting platform, since TLS terminates at their edge and the container is
            // spoken to over plain http.
            ->withSecure($request->isSecure());
    }
}

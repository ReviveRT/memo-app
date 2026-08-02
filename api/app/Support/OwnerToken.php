<?php

declare(strict_types=1);

namespace App\Support;

use Random\RandomException;
use RuntimeException;

/**
 * The secret a browser holds to prove which memos are its own.
 *
 * There is no account behind it. This token *is* the identity: whoever presents it is the
 * owner, which makes it a bearer credential and puts it in the same class of thing as a
 * password, not in the same class as a user id. Three consequences follow.
 *
 *   * It has to be unguessable, not merely unique. Str::uuid7() -- which mints every other
 *     id in this application -- is exactly the wrong generator for it: 48 of its bits are
 *     a millisecond timestamp and another handful are version and variant, so a v7 leaks
 *     roughly when it was created and offers well under half its width to an attacker who
 *     has to guess. This uses random_bytes, which is the CSPRNG, and 128 bits of it.
 *   * It must not be stored. db/migrations/007_owners.sql keeps only hash() of it, so this
 *     class is the only place the plaintext exists on the server, and only for the length
 *     of one request.
 *   * **Two of these must never be compared with `===`.** Nothing does, and that is a
 *     property of the design rather than of anybody's care: a cookie is resolved by looking
 *     up its *hash*, so the database does the matching and no plaintext comparison happens
 *     anywhere in the request path. Should a reason to compare two tokens ever appear, it is
 *     `hash_equals` -- `===` on a secret leaks its prefix through timing.
 *
 * @see \App\Http\Middleware\ResolveOwner for where a token becomes a request's owner.
 */
final class OwnerToken
{
    /**
     * 16 bytes = 128 bits.
     *
     * The figure that matters is not the table size but the guess rate: a token is checked
     * by one indexed lookup, so an attacker is bounded by how fast they can make HTTP
     * requests. At an implausible million attempts per second, exhausting a millionth of
     * this keyspace still takes longer than the universe has existed. 256 bits would be
     * free to store and is not more secure in any sense that can be acted on; it would just
     * make the claim URL longer to paste.
     */
    private const BYTES = 16;

    /**
     * base64url of 16 bytes, unpadded. Fixed length, so this is `{22}` rather than `+`.
     *
     * Anchored with \A and \z rather than ^ and $, which is load-bearing on a value that
     * arrives from a URL: $ also matches immediately before a trailing newline, so the
     * obvious spelling would accept "validtoken\n" as valid and then look up a string that
     * is not in the table -- turning a rejected input into a confusing 404.
     */
    private const FORMAT = '/\A[A-Za-z0-9_-]{22}\z/';

    /**
     * Never instantiated. Static because a token has no state worth wrapping -- the string
     * is the whole of it, and a value object here would only mean unwrapping it at every
     * call site.
     */
    private function __construct() {}

    /**
     * A fresh token.
     *
     * base64url rather than plain base64, because this string's destination is a URL path
     * segment in the claim link. Plain base64's `+` and `/` would both need escaping there,
     * and a `/` would split the segment in two; the padding `=` is stripped for the same
     * reason and costs nothing, since the length is fixed and known.
     *
     * @throws RuntimeException When the system CSPRNG is unavailable. Deliberately not
     *                          caught and downgraded to a weaker source: a predictable
     *                          token here is indistinguishable from no security at all, and
     *                          a 500 is the correct answer to a machine that cannot produce
     *                          randomness.
     */
    public static function mint(): string
    {
        try {
            $bytes = random_bytes(self::BYTES);
        } catch (RandomException $e) {
            throw new RuntimeException('Could not generate an owner token: no secure randomness available.', 0, $e);
        }

        return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
    }

    /**
     * Whether a string is shaped like a token, checked before it is used as one.
     *
     * This is not a security boundary -- a well-formed token that belongs to nobody is
     * refused by the lookup either way. It is here so that garbage in a URL is refused
     * without a database round trip, and so the claim route can tell "that is not a token"
     * apart from "that token has expired", which are different sentences to show someone.
     */
    public static function isWellFormed(string $token): bool
    {
        return preg_match(self::FORMAT, $token) === 1;
    }

    /**
     * What db/migrations/007_owners.sql stores: lowercase hex SHA-256.
     *
     * A plain fast hash, and the migration has the argument for why that is right rather
     * than lazy here: password hashes are slow to make *guessing* expensive, and there is
     * nothing to guess in 128 CSPRNG bits. Slowing this down would add latency to a lookup
     * on every request and buy nothing.
     *
     * Agreement with Postgres is exact and was verified rather than assumed --
     * `encode(sha256(convert_to(t, 'UTF8')), 'hex')` and this call produce the same 64
     * characters for the same input, which is what lets the migration's backfill mint a
     * token PHP can later recognise.
     */
    public static function hash(string $token): string
    {
        return hash('sha256', $token);
    }

}

<?php

declare(strict_types=1);

namespace App\Http\Rules;

use Closure;
use Illuminate\Contracts\Validation\ValidationRule;

/**
 * Refuses a NUL anywhere in a string, because nothing downstream will refuse it for us
 * -- it is silently destructive rather than fatal.
 *
 * Postgres itself does reject a null character in `text` (SQLSTATE 54000, "null
 * character not permitted"), which is what made this look unnecessary. It never gets the
 * chance: libpq passes bound parameters as C strings, so the value is truncated at the
 * first NUL before the server sees it. Verified through PDO -- `SELECT length(?::text)`
 * bound with "a\0b" returns 1.
 *
 * A rule of its own rather than a closure in each FormRequest, because the truncation is
 * the driver's behaviour rather than either field's, and the two consequences are equally
 * quiet:
 *
 *   * StoreMemoRequest. A three-character POST answered 201 with a one-character
 *     transcript -- the memo is thrown away and the response says it was stored.
 *   * ListMemosRequest. The filter runs against a prefix of what was typed, so the list
 *     comes back for a query nobody asked for and nothing on the page says so.
 *
 * Both fields are also trimmed before validation, and PHP's default trim charlist
 * contains "\0" -- so a NUL at either edge is already gone by the time this runs, and the
 * interior case is the only one it exists for. That is why the trim cannot stand in for
 * it.
 *
 * Refused rather than stripped: the same call LocalAudioStorage::path() makes, for the
 * same reason. Quietly editing what somebody typed is not better than declining it.
 */
final class NoNullBytes implements ValidationRule
{
    public function validate(string $attribute, mixed $value, Closure $fail): void
    {
        if (is_string($value) && str_contains($value, "\0")) {
            $fail('The :attribute field must not contain null bytes.');
        }
    }
}

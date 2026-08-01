<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Support\Carbon;

/**
 * A half-open interval of time, ready to bind: `created_at >= from AND created_at < to`.
 *
 * Both ends are optional, so this covers "since Monday", "up to Friday", "between the
 * two" and "no filter at all" without any caller branching on four combinations.
 *
 * **Half-open, and that is the contract rather than an implementation detail.** `from`
 * is inclusive and `to` is exclusive, so a request for one day passes midnight to
 * midnight of the *following* day. The obvious alternative -- an inclusive `to` at
 * 23:59:59 -- silently loses every row written in the last second of the day, and at
 * millisecond precision (which is what `to_char` hands out and what a browser's
 * toISOString produces) it loses the last 999 milliseconds too. Nothing surfaces: the
 * list is simply short, on the boundary, for the newest rows in the range.
 *
 * **The timezone belongs to the client, not to this class.** "Yesterday" is a local
 * question -- the same instant is Sunday in Auckland and Saturday in Los Angeles -- and
 * the API has no way to know which one was meant. So the frontend computes local
 * midnight, converts to an instant, and sends that; this normalises whatever arrives to
 * UTC and compares instants. The API therefore has no timezone opinion to get wrong, and
 * there is no `tz` parameter to keep in step with the browser's.
 *
 * A value with no offset -- `2026-07-19T00:00:00` -- is read as UTC, because
 * config/app.php sets the application timezone to UTC and Carbon parses against it. That
 * is a defensible reading rather than a good request, and the frontend never makes it:
 * `Date#toISOString` always carries the Z.
 */
final class TimeWindow
{
    /**
     * @param  ?string  $from  Inclusive, already normalised to UTC, or null for unbounded.
     * @param  ?string  $to  Exclusive, already normalised to UTC, or null for unbounded.
     */
    private function __construct(
        public readonly ?string $from,
        public readonly ?string $to,
    ) {}

    /** No bound at either end -- the whole table. */
    public static function unbounded(): self
    {
        return new self(null, null);
    }

    /**
     * Normalises two optional instants into a window.
     *
     * Parsing is trusted here because the ordering and the format have already been
     * settled by validation -- FiltersByTime carries the rules, including that `to` must
     * be strictly after `from`. Called with unvalidated input, Carbon throws, which is
     * the right outcome for a caller that skipped the FormRequest.
     */
    public static function between(?string $from, ?string $to): self
    {
        return new self(self::instant($from), self::instant($to));
    }

    /** Whether this bounds anything at all. Both ends absent means no window clause. */
    public function isUnbounded(): bool
    {
        return $this->from === null && $this->to === null;
    }

    /**
     * ISO 8601 in UTC with an explicit offset, which is the one spelling of an instant
     * Postgres cannot misread.
     *
     * A bare `2026-07-19 00:00:00` bound to a timestamptz is resolved against the
     * *server's* TimeZone setting, so the same request would mean different rows on two
     * differently configured databases. The offset is what removes that variable, and it
     * is the same reason MemoRepository::COLUMNS formats on the way out with
     * `AT TIME ZONE 'UTC'` rather than letting DateStyle decide.
     *
     * Microsecond precision, because `created_at` is a timestamptz and Postgres keeps
     * microseconds. Truncating to seconds here would put the boundary up to a second
     * away from where the caller asked for it.
     */
    private static function instant(?string $value): ?string
    {
        if ($value === null || $value === '') {
            return null;
        }

        return Carbon::parse($value)->utc()->format('Y-m-d\TH:i:s.uP');
    }
}

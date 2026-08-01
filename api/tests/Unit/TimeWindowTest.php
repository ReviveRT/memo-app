<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Support\TimeWindow;
use PHPUnit\Framework\TestCase;

/**
 * The date filter's contract, which two runtimes have to agree about: the browser builds
 * these bounds and Postgres compares against them.
 *
 * Pure, so it needs neither a framework nor a database -- PHPUnit's TestCase rather than the
 * application's. What it cannot check is the half-openness itself, because that lives in the
 * SQL (`created_at >= from AND created_at < to`, in MemoRepository::list). What it pins here
 * is the part a mistake would be silent in: that both ends arrive at Postgres as unambiguous
 * UTC instants.
 */
final class TimeWindowTest extends TestCase
{
    public function test_an_unbounded_window_binds_nothing(): void
    {
        $window = TimeWindow::unbounded();

        $this->assertNull($window->from);
        $this->assertNull($window->to);
        $this->assertTrue($window->isUnbounded());
    }

    public function test_both_spellings_of_absent_collapse_to_null(): void
    {
        // `?from=` is the ordinary output of a frontend building a query string with no date
        // chosen, and it has to mean the same thing as omitting the parameter. Collapsing it
        // here rather than in the repository is what keeps every layer below from having to
        // decide whether '' is a bound.
        $window = TimeWindow::between(null, '');

        $this->assertTrue($window->isUnbounded());
    }

    public function test_an_instant_is_normalised_to_utc_with_an_explicit_offset(): void
    {
        // The whole point of the normalisation. A bare timestamp bound to a timestamptz is
        // resolved against the *server's* TimeZone setting, so the identical request would
        // select different rows on two differently configured databases.
        $window = TimeWindow::between('2026-07-19T00:00:00+02:00', '2026-07-24T00:00:00Z');

        $this->assertSame('2026-07-18T22:00:00.000000+00:00', $window->from);
        $this->assertSame('2026-07-24T00:00:00.000000+00:00', $window->to);
        $this->assertFalse($window->isUnbounded());
    }

    public function test_a_value_with_no_offset_is_read_as_utc(): void
    {
        // Defensible rather than good input, and the frontend never sends it --
        // Date#toISOString always carries the Z. Pinned so the reading is a decision:
        // config/app.php sets the application timezone to UTC and Carbon parses against it.
        $this->assertSame(
            '2026-07-19T00:00:00.000000+00:00',
            TimeWindow::between('2026-07-19T00:00:00', null)->from,
        );
    }

    public function test_microseconds_survive(): void
    {
        // created_at is a timestamptz and Postgres keeps microseconds, so truncating to
        // seconds here would put the boundary up to a second away from where the caller asked
        // for it -- which at a day boundary is a memo on the wrong side of the filter.
        $this->assertSame(
            '2026-07-19T00:00:00.123456+00:00',
            TimeWindow::between('2026-07-19T00:00:00.123456Z', null)->from,
        );
    }

    public function test_one_open_end_is_a_valid_window(): void
    {
        // "Everything since Monday" is a filter somebody means to apply, so a single bound
        // must not be treated as no bound.
        $since = TimeWindow::between('2026-07-19T00:00:00Z', null);

        $this->assertNotNull($since->from);
        $this->assertNull($since->to);
        $this->assertFalse($since->isUnbounded());

        $until = TimeWindow::between(null, '2026-07-24T00:00:00Z');

        $this->assertNull($until->from);
        $this->assertNotNull($until->to);
        $this->assertFalse($until->isUnbounded());
    }
}

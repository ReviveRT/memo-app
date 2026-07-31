<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Repositories\MemoRepository;
use PHPUnit\Framework\TestCase;

/**
 * MemoRepository::likePattern -- the ILIKE half of the search predicate.
 *
 * A unit test rather than part of MEMO-25's database suite, and that is the point of the
 * method being public and static: what has to be right is the string, and asserting the
 * string says which characters are escaped and why. Asserting the row count instead would
 * need a live Postgres and would still not say what went wrong when it changed.
 */
final class LikePatternTest extends TestCase
{
    public function test_a_plain_query_becomes_a_substring_pattern(): void
    {
        // The case the fallback exists for: five characters that no tsquery will match
        // against a transcript saying "reorganise".
        $this->assertSame('%reorg%', MemoRepository::likePattern('reorg'));
    }

    public function test_a_percent_in_the_query_is_not_a_wildcard(): void
    {
        // The bug this pins, measured against a 5,006-row fixture: unescaped, `50%`
        // builds `%50%%`, whose trailing pair reads as "anything", and the search
        // returned 102 rows instead of the one memo mentioning a 50% margin.
        $this->assertSame('%50\%%', MemoRepository::likePattern('50%'));

        // A query that is nothing but a wildcard must match memos containing a literal
        // percent sign, not every memo there is.
        $this->assertSame('%\%%', MemoRepository::likePattern('%'));
    }

    public function test_an_underscore_in_the_query_is_not_a_single_character_wildcard(): void
    {
        // The quieter version of the same bug: `_` matches exactly one character, so
        // unescaped this would make `created_at` and `createdXat` the same query. Easy to
        // reach from a memo about code.
        $this->assertSame('%created\_at%', MemoRepository::likePattern('created_at'));
    }

    public function test_a_backslash_is_escaped_before_the_wildcards_it_would_otherwise_escape(): void
    {
        // Backslash is LIKE's default escape character. A lone trailing one leaves a
        // dangling escape and Postgres raises on the pattern, so this is the character
        // whose mishandling is a 500 rather than a wrong row count.
        $this->assertSame('%\\\\%', MemoRepository::likePattern('\\'));

        // Order matters, and this is the assertion that pins it: the backslash the user
        // typed is doubled, and the percent is escaped with one backslash of its own. If
        // `%` were escaped first, this step would then double the backslash it had just
        // added and the pattern would search for a literal backslash followed by
        // anything.
        $this->assertSame('%\\\\\\%%', MemoRepository::likePattern('\\%'));
    }

    public function test_the_rest_of_a_query_is_passed_through_untouched(): void
    {
        // Everything websearch_to_tsquery treats as syntax is literal to ILIKE, and that
        // is left alone rather than stripped. The fallback is inert for these queries --
        // no transcript contains `dentist -thursday` as a substring -- and the full-text
        // arm is what serves them.
        $this->assertSame('%"call the dentist"%', MemoRepository::likePattern('"call the dentist"'));
        $this->assertSame('%dentist -thursday%', MemoRepository::likePattern('dentist -thursday'));

        // Multibyte input is bytes to str_replace and none of them are the three
        // characters being escaped, so a non-ASCII query survives intact.
        $this->assertSame('%歯医者%', MemoRepository::likePattern('歯医者'));
    }
}

<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Services\Memos\Memo;
use JsonException;
use PHPUnit\Framework\TestCase;
use RuntimeException;
use stdClass;

/**
 * Row-to-response mapping. Pure, so it needs neither a framework nor a database --
 * PHPUnit's TestCase rather than the application's.
 */
final class MemoTest extends TestCase
{
    public function test_the_pinned_statuses_are_every_status_that_is_not_terminal(): void
    {
        $this->assertSame(['queued', 'processing'], Memo::inFlightStatuses());

        // Derived, not listed, and this is the property that matters: a status added to
        // the lifecycle is pinned into a filtered page unless it is explicitly declared
        // terminal. The alternative -- a hand-written list of unfinished statuses -- is
        // one value short the moment MEMO-16 adds its retry status, and the symptom is
        // silent: a memo that is still being worked on simply stops appearing while a
        // filter is active. Unknown must mean not finished.
        $this->assertSame(
            [],
            array_intersect(Memo::inFlightStatuses(), Memo::TERMINAL_STATUSES),
            'A terminal status must never be pinned.',
        );

        $this->assertSame(
            Memo::STATUSES,
            array_merge(Memo::inFlightStatuses(), Memo::TERMINAL_STATUSES),
            'Every allowed status must be classified as exactly one of the two.',
        );

        // Positional bindings are spread from this, so a gapped array would bind in an
        // order nobody intended. array_diff preserves keys; array_values is what removes
        // them.
        $this->assertSame(
            array_keys(Memo::inFlightStatuses()),
            range(0, count(Memo::inFlightStatuses()) - 1),
            'The list must be a list, not a gapped array.',
        );
    }

    public function test_the_response_carries_exactly_the_documented_fields(): void
    {
        // Asserted as an ordered whole rather than field by field, because the thing
        // worth pinning is the absence of the columns that are not here: search_vector
        // (the largest thing on the row), the queue bookkeeping, and enrichment_error.
        $this->assertSame(
            [
                'id', 'source', 'status', 'transcript', 'title',
                'summary', 'tags', 'duration_ms', 'last_error', 'created_at',
            ],
            array_keys(Memo::fromRow($this->row())->toArray()),
        );
    }

    public function test_tags_arrive_as_a_json_array_and_become_a_list_of_strings(): void
    {
        $memo = Memo::fromRow($this->row(['tags' => '["dentist", "health"]']));

        $this->assertSame(['dentist', 'health'], $memo->tags);
        $this->assertSame(['dentist', 'health'], $memo->toArray()['tags']);
    }

    public function test_a_tag_containing_a_comma_or_a_quote_survives(): void
    {
        // The reason the query asks for to_jsonb(tags) instead of letting the driver
        // hand over Postgres' own `{a,b}` literal. In that literal this pair arrives
        // as {"dentist, urgent","say \"ah\""} -- splitting on commas would produce
        // three tags, two of them wrong. MEMO-21 generates these from model output,
        // so a tag with punctuation in it is expected rather than hypothetical.
        $memo = Memo::fromRow($this->row([
            'tags' => '["dentist, urgent", "say \"ah\""]',
        ]));

        $this->assertSame(['dentist, urgent', 'say "ah"'], $memo->tags);
    }

    public function test_no_tags_is_an_empty_list_rather_than_null(): void
    {
        // The column is NOT NULL DEFAULT '{}', so this is what a fresh memo looks
        // like. It has to stay a JSON array: a frontend iterating tags should not
        // have to null-check first.
        $this->assertSame([], Memo::fromRow($this->row(['tags' => '[]']))->tags);
    }

    public function test_unparseable_tags_fail_loudly_instead_of_silently_emptying(): void
    {
        // json_decode's own answer to bad input is null, which would look exactly
        // like a memo that has no tags.
        $this->expectException(JsonException::class);

        Memo::fromRow($this->row(['tags' => 'not json']));
    }

    public function test_an_unknown_duration_stays_null_and_is_not_reported_as_zero(): void
    {
        // The null check, not the cast, is the part that matters: (int) null is 0, and
        // a text memo reported as 0 would render as a 0:00 clip rather than as a memo
        // with no audio at all.
        $this->assertNull(Memo::fromRow($this->row(['duration_ms' => null]))->durationMs);

        // A genuine zero still has to survive as zero, which is what stops the null
        // check being written as a falsy test.
        $this->assertSame(0, Memo::fromRow($this->row(['duration_ms' => 0]))->durationMs);
    }

    public function test_a_duration_arriving_as_a_string_becomes_an_integer(): void
    {
        // pdo_pgsql returns native ints on PHP 8.1+, so this is belt and braces --
        // it keeps duration_ms typed as a number in the JSON regardless.
        $memo = Memo::fromRow($this->row(['duration_ms' => '4200']));

        $this->assertSame(4200, $memo->durationMs);
    }

    public function test_the_formatted_timestamp_is_read_from_created_at_iso(): void
    {
        // The alias is deliberately not `created_at`; a bare `created_at` in the list
        // query's ORDER BY would bind to the output label instead of the column and
        // lose memos_created_idx. This asserts the DTO reads the name the query
        // actually produces.
        $this->assertSame(
            '2026-07-31T09:00:00.000Z',
            Memo::fromRow($this->row())->toArray()['created_at'],
        );
    }

    public function test_a_projection_that_no_longer_matches_the_dto_fails_immediately(): void
    {
        // Renaming a column on one side of the seam must not ship a response with an
        // empty field in it. Reading a missing property off stdClass is only a warning.
        $row = $this->row();
        unset($row->created_at_iso);

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('created_at_iso');

        Memo::fromRow($row);
    }

    public function test_the_nullable_enrichment_fields_pass_through_as_null(): void
    {
        $memo = Memo::fromRow($this->row([
            'title' => null,
            'summary' => null,
            'last_error' => null,
        ]));

        $this->assertNull($memo->title);
        $this->assertNull($memo->summary);
        $this->assertNull($memo->lastError);
    }

    /**
     * One row of MemoRepository::COLUMNS, as Laravel's FETCH_OBJ hands it over.
     *
     * @param  array<string, mixed>  $overrides
     */
    private function row(array $overrides = []): stdClass
    {
        return (object) array_merge([
            'id' => '019fb4ef-0d71-7011-b678-0cb4004dc2a7',
            'source' => 'text',
            'status' => 'queued',
            'transcript' => 'Call the dentist on Tuesday',
            'title' => 'Dentist',
            'summary' => 'A reminder to call the dentist.',
            'tags' => '["dentist"]',
            'duration_ms' => null,
            'last_error' => null,
            // Already formatted; the query does this with to_char so nothing here has
            // to guess at the server's DateStyle. Named created_at_iso rather than
            // created_at so that ORDER BY in the list query cannot bind to it -- see
            // MemoRepository::COLUMNS.
            'created_at_iso' => '2026-07-31T09:00:00.000Z',
        ], $overrides);
    }
}

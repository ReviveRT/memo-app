<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;
use Tests\Support\FakeMemoRepository;
use Tests\TestCase;

/**
 * The filters the memo list grew: a date window, a collection scope, and the PATCH that moves
 * a memo between collections.
 *
 * A file of its own rather than more of MemoEndpointsTest, which is already 600 lines about
 * creating memos and searching them. These are about narrowing the list and about one write
 * that MemoEndpointsTest's subject -- MEMO-06 and MEMO-10 -- does not cover.
 *
 * What is asserted is everything up to the SQL: that the query string is read the way the
 * contract says, that the MemoQuery reaching the repository is the one the parameters
 * describe, and that a bad request is refused in words a reader can act on. The statement
 * itself needs a live Postgres -- see FakeMemoRepository for why the seam is here -- and the
 * plans and the in-flight pin's scoping were measured against one while this was built.
 */
final class MemoFilterEndpointsTest extends TestCase
{
    private FakeMemoRepository $repository;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);
    }

    public function test_a_date_range_reaches_the_repository_as_utc_instants(): void
    {
        $this->getJson('/api/memos?from=2026-07-19T00:00:00%2B02:00&to=2026-07-24T00:00:00Z')
            ->assertOk();

        $window = $this->repository->lastQuery?->window;

        // Normalised, not passed through: the offset form and the Z form both have to reach
        // Postgres as the same unambiguous instant. See App\Support\TimeWindow.
        $this->assertSame('2026-07-18T22:00:00.000000+00:00', $window?->from);
        $this->assertSame('2026-07-24T00:00:00.000000+00:00', $window?->to);
    }

    public function test_the_filters_are_echoed_as_they_were_sent(): void
    {
        // As sent, not as normalised. The caption reads in the reader's own timezone, and a
        // UTC instant is not what it needs to say "19 Jul - 23 Jul"; the normalisation exists
        // for comparing rows.
        $this->getJson('/api/memos?from=2026-07-19T00:00:00%2B02:00&to=2026-07-24T00:00:00Z&collection=none')
            ->assertOk()
            ->assertJsonPath('from', '2026-07-19T00:00:00+02:00')
            ->assertJsonPath('to', '2026-07-24T00:00:00Z')
            ->assertJsonPath('collection', 'none');

        // Always present, so the client reads four keys rather than branching on whether each
        // exists.
        $this->getJson('/api/memos')
            ->assertOk()
            ->assertJsonPath('from', null)
            ->assertJsonPath('to', null)
            ->assertJsonPath('collection', null);
    }

    public function test_one_open_end_is_a_window_and_neither_end_is_not(): void
    {
        $this->getJson('/api/memos?from=2026-07-19T00:00:00Z')->assertOk();
        $this->assertNotNull($this->repository->lastQuery?->window->from);
        $this->assertNull($this->repository->lastQuery?->window->to);

        $this->getJson('/api/memos')->assertOk();
        $this->assertTrue($this->repository->lastQuery?->window->isUnbounded());
    }

    public function test_a_blank_date_is_no_bound_rather_than_a_422(): void
    {
        // `?from=&to=` is what a query string built around two empty date inputs looks like,
        // and it is the ordinary request rather than the edge case.
        $this->getJson('/api/memos?from=&to=')->assertOk();

        $this->assertTrue($this->repository->lastQuery?->window->isUnbounded());
    }

    public function test_an_inverted_or_empty_range_is_refused_in_words_a_reader_can_act_on(): void
    {
        // Both are refused rather than answered with an empty list, because an empty list is
        // exactly how this bug hides -- the likeliest cause is a date picker that forgot to
        // advance the exclusive `to` bound by a day for a single-day range.
        foreach (
            [
                'from=2026-07-24T00:00:00Z&to=2026-07-19T00:00:00Z',
                'from=2026-07-19T00:00:00Z&to=2026-07-19T00:00:00Z',
            ] as $range
        ) {
            $response = $this->getJson("/api/memos?{$range}");

            $response->assertStatus(422)->assertJsonValidationErrors('to');

            $this->assertStringContainsString('after the start', (string) $response->json('message'));
        }

        // Neither reached the database.
        $this->assertNull($this->repository->lastQuery);
    }

    public function test_an_unparseable_date_names_the_control_rather_than_the_parameter(): void
    {
        // The frontend renders a failed GET's `message` verbatim, and "The from field is not a
        // valid date" describes a query-string parameter the person filtering never typed.
        $response = $this->getJson('/api/memos?from=not-a-date');

        $response->assertStatus(422)->assertJsonValidationErrors('from');

        $this->assertStringContainsString('date range', (string) $response->json('message'));
        $this->assertStringNotContainsString('from field', (string) $response->json('message'));
    }

    public function test_the_collection_scope_has_three_readings_and_cannot_say_two_at_once(): void
    {
        // Absent: not scoped.
        $this->getJson('/api/memos')->assertOk();
        $this->assertNull($this->repository->lastQuery?->collectionId);
        $this->assertFalse($this->repository->lastQuery?->unfiledOnly);

        // `none`: the fast strip. Reconciled into `unfiledOnly` here rather than downstream,
        // so no layer below can be handed a null collection id that might mean either thing.
        $this->getJson('/api/memos?collection=none')->assertOk();
        $this->assertNull($this->repository->lastQuery?->collectionId);
        $this->assertTrue($this->repository->lastQuery?->unfiledOnly);

        // An id: one collection.
        $id = '019fb4f0-0d71-7011-b678-0cb4004dc2a7';
        $this->getJson("/api/memos?collection={$id}")->assertOk();
        $this->assertSame($id, $this->repository->lastQuery?->collectionId);
        $this->assertFalse($this->repository->lastQuery?->unfiledOnly);
    }

    public function test_a_collection_scope_that_is_neither_an_id_nor_none_is_refused(): void
    {
        $response = $this->getJson('/api/memos?collection=banana');

        $response->assertStatus(422)->assertJsonValidationErrors('collection');

        // The message names both of the things it accepts, because the field's own rule is
        // the only explanation the caller gets.
        $this->assertStringContainsString('none', (string) $response->json('message'));
    }

    public function test_every_filter_applies_at_once(): void
    {
        $id = '019fb4f0-0d71-7011-b678-0cb4004dc2a7';

        $this->getJson("/api/memos?q=dentist&from=2026-07-19T00:00:00Z&to=2026-07-24T00:00:00Z&collection={$id}&limit=10")
            ->assertOk();

        $query = $this->repository->lastQuery;

        // All four are independent, which is why the repository assembles one statement from
        // optional predicates rather than offering a method per combination.
        $this->assertSame('dentist', $query?->text);
        $this->assertSame($id, $query?->collectionId);
        $this->assertSame(10, $query?->limit);
        $this->assertFalse($query?->window->isUnbounded());
    }

    public function test_moving_a_memo_into_a_collection_answers_with_the_memo(): void
    {
        $id = '019fb4ef-0d71-7011-b678-0cb4004dc2a7';
        $collection = '019fb4f0-0d71-7011-b678-0cb4004dc2a7';

        $this->repository->moveResult = $this->memo($id, $collection);

        $this->patchJson("/api/memos/{$id}", ['collection_id' => $collection])
            ->assertOk()
            // The whole memo, not an acknowledgement: the frontend reconciles its lists by id,
            // so a route that returns the row in its new state needs no follow-up GET.
            ->assertJsonPath('memo.id', $id)
            ->assertJsonPath('memo.collection_id', $collection);

        $this->assertSame([[$id, $collection]], $this->repository->moved);
    }

    public function test_a_null_collection_unfiles_the_memo_rather_than_meaning_nothing(): void
    {
        $id = '019fb4ef-0d71-7011-b678-0cb4004dc2a7';

        $this->repository->moveResult = $this->memo($id, null);

        $this->patchJson("/api/memos/{$id}", ['collection_id' => null])
            ->assertOk()
            ->assertJsonPath('memo.collection_id', null);

        // Null reached the repository as a value. This is how a memo filed by mistake gets
        // back out, so it has to be a real operation rather than an omitted argument.
        $this->assertSame([[$id, null]], $this->repository->moved);
    }

    public function test_a_patch_naming_no_field_is_refused_rather_than_silently_doing_nothing(): void
    {
        // A PATCH that silently no-ops is the worst outcome available: the client believes the
        // move happened. This used to be `present` on `collection_id`; with a second writable
        // field that rule became wrong -- a rename would have to send a collection to say
        // "leave it where it is" -- so the guarantee moved to a check over the whole body.
        $response = $this->patchJson('/api/memos/019fb4ef-0d71-7011-b678-0cb4004dc2a7', []);

        $response->assertStatus(422);

        $this->assertStringContainsString('no change', (string) $response->json('message'));
        $this->assertSame([], $this->repository->moved);
        $this->assertSame([], $this->repository->renamed);
    }

    public function test_a_missing_memo_or_collection_is_one_404(): void
    {
        // The repository cannot tell the two apart and deliberately does not try: both are the
        // client naming something that is not there, and the remedy is the same.
        $this->repository->moveResult = null;

        $this->patchJson('/api/memos/019fb4ef-0d71-7011-b678-0cb4004dc2a7', ['collection_id' => null])
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo or collection no longer exists. Refresh and try again.');
    }

    public function test_an_id_that_is_not_a_uuid_never_reaches_the_controller(): void
    {
        // whereUuid on the route. Without it the value would be handed to Postgres and come
        // back as a 500 from `invalid input syntax for type uuid`; constrained, it matches no
        // route and answers the 404 a nonexistent memo should.
        $this->patchJson('/api/memos/not-a-uuid', ['collection_id' => null])->assertNotFound();

        $this->assertSame([], $this->repository->moved);
    }

    /** A memo shaped like the row Postgres would have returned. */
    private function memo(string $id, ?string $collectionId): Memo
    {
        return new Memo(
            id: $id,
            source: 'text',
            status: 'ready',
            transcript: 'Call the dentist on Tuesday',
            title: 'Dentist',
            summary: null,
            tags: [],
            category: null,
            durationMs: null,
            lastError: null,
            lastErrorCode: null,
            createdAt: '2026-07-31T09:00:00.000Z',
            collectionId: $collectionId,
            reminders: [],
        );
    }
}

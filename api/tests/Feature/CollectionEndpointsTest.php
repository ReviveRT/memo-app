<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Http\Requests\SaveCollectionRequest;
use App\Repositories\CollectionRepository;
use App\Services\Collections\Collection;
use Tests\Support\FakeCollectionRepository;
use Tests\TestCase;

/**
 * The four collection routes: list, create, rename, delete.
 *
 * What is asserted is the half that does not depend on the driver -- the validation, the
 * response shape, and the three-way translation of the repository's answers into 200, 404 and
 * 422. The SQL is verified against a live Postgres; see FakeCollectionRepository for what that
 * covered and what it cannot.
 */
final class CollectionEndpointsTest extends TestCase
{
    private FakeCollectionRepository $repository;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeCollectionRepository;
        $this->app->instance(CollectionRepository::class, $this->repository);
    }

    public function test_the_grid_carries_what_a_card_draws(): void
    {
        $this->repository->rows = [$this->collection('Memos for Work', 2, ['Standup Notes', 'Sunday Meeting'])];

        $this->getJson('/api/collections')
            ->assertOk()
            ->assertJsonCount(1, 'collections')
            ->assertJsonPath('collections.0.name', 'Memos for Work')
            // A number, not a string. count(*) is a bigint and pdo_pgsql hands those over as
            // strings, so the cast in Collection::fromRow is what keeps `count === 0` working
            // on the client.
            ->assertJsonPath('collections.0.memo_count', 2)
            ->assertJsonPath('collections.0.recent_labels', ['Standup Notes', 'Sunday Meeting']);
    }

    public function test_the_list_takes_the_same_filters_the_memo_list_takes(): void
    {
        // The brief asks for one filter that behaves the same over collections as over memos,
        // so the parameters are spelled identically and mean the same things. A shared date
        // picker in the UI sends the same query string to both routes.
        $this->getJson('/api/collections?q=work&from=2026-07-19T00:00:00Z&to=2026-07-24T00:00:00Z')
            ->assertOk()
            ->assertJsonPath('query', 'work')
            ->assertJsonPath('from', '2026-07-19T00:00:00Z')
            ->assertJsonPath('to', '2026-07-24T00:00:00Z');

        [$text, $window] = $this->repository->lastList;

        $this->assertSame('work', $text);
        $this->assertSame('2026-07-19T00:00:00.000000+00:00', $window->from);
    }

    public function test_a_blank_query_is_no_filter_and_an_inverted_range_is_refused(): void
    {
        $this->getJson('/api/collections?q=%20%20')->assertOk()->assertJsonPath('query', null);

        $this->assertNull($this->repository->lastList[0]);

        $this->getJson('/api/collections?from=2026-07-24T00:00:00Z&to=2026-07-19T00:00:00Z')
            ->assertStatus(422)
            ->assertJsonValidationErrors('to');
    }

    public function test_creating_answers_201_with_the_stored_collection(): void
    {
        $this->repository->writeResult = $this->collection('Memos for Work', 0, []);

        $this->postJson('/api/collections', ['name' => 'Memos for Work'])
            ->assertCreated()
            ->assertJsonPath('collection.name', 'Memos for Work')
            // 0 and [] are what a new collection is, and the client prepends the card without
            // a follow-up GET.
            ->assertJsonPath('collection.memo_count', 0)
            ->assertJsonPath('collection.recent_labels', []);

        $this->assertSame('Memos for Work', $this->repository->inserted[0][1]);
    }

    public function test_the_name_is_trimmed_before_it_is_stored(): void
    {
        // So the length rule and the uniqueness index judge the same string the database will
        // hold -- and so a collection is never stored with padding it will then never match
        // its own name by.
        $this->repository->writeResult = $this->collection('Work', 0, []);

        $this->postJson('/api/collections', ['name' => '  Work  '])->assertCreated();

        $this->assertSame('Work', $this->repository->inserted[0][1]);
    }

    public function test_a_blank_name_is_a_422_rather_than_a_constraint_violation(): void
    {
        // Untrimmed, "  " satisfies `required` and reaches Postgres, where the CHECK on
        // `btrim(name) <> ''` refuses it as an unhandled QueryException -- a 500 for what is
        // plainly a form error.
        foreach (['', '   ', "\t"] as $blank) {
            $this->postJson('/api/collections', ['name' => $blank])
                ->assertStatus(422)
                ->assertJsonValidationErrors('name');
        }

        $this->assertSame([], $this->repository->inserted);
    }

    public function test_an_oversized_or_null_byte_name_is_rejected(): void
    {
        $this->postJson('/api/collections', ['name' => str_repeat('a', SaveCollectionRequest::MAX_NAME_LENGTH + 1)])
            ->assertStatus(422)
            ->assertJsonValidationErrors('name');

        // libpq truncates a bound parameter at the first NUL, so without this a collection
        // named "Wo\0rk" would be stored as "Wo" and the 201 would report a name the user did
        // not choose.
        $this->postJson('/api/collections', ['name' => "Wo\0rk"])
            ->assertStatus(422)
            ->assertJsonValidationErrors('name');

        $this->assertSame([], $this->repository->inserted);

        // Exactly at the cap passes, so the boundary is a cap and not an off-by-one.
        $this->repository->writeResult = $this->collection('x', 0, []);
        $this->postJson('/api/collections', ['name' => str_repeat('a', SaveCollectionRequest::MAX_NAME_LENGTH)])
            ->assertCreated();
    }

    public function test_a_duplicate_name_is_a_422_naming_the_collection(): void
    {
        // The likeliest cause is a collection the user has already made and forgotten, and the
        // grid may well be filtered so that it is not on screen -- so the message repeats the
        // name rather than saying "already exists".
        $this->repository->writeResult = false;

        $response = $this->postJson('/api/collections', ['name' => 'Memos for Work']);

        $response->assertStatus(422)->assertJsonValidationErrors('name');

        $this->assertStringContainsString('Memos for Work', (string) $response->json('message'));
    }

    public function test_renaming_answers_with_the_collection(): void
    {
        $this->repository->writeResult = $this->collection('Work', 2, ['Standup Notes']);

        $this->patchJson('/api/collections/019fb4f0-0d71-7011-b678-0cb4004dc2a7', ['name' => 'Work'])
            ->assertOk()
            ->assertJsonPath('collection.name', 'Work')
            // The count and labels survive a rename, so the card does not blank out and refill.
            ->assertJsonPath('collection.memo_count', 2);
    }

    public function test_renaming_tells_a_taken_name_apart_from_a_missing_collection(): void
    {
        // Collapsing these -- which one `if (! $renamed)` would do -- tells somebody who typed
        // a duplicate name that their collection has disappeared.
        $this->repository->writeResult = false;

        $this->patchJson('/api/collections/019fb4f0-0d71-7011-b678-0cb4004dc2a7', ['name' => 'Errands'])
            ->assertStatus(422)
            ->assertJsonValidationErrors('name');

        $this->repository->writeResult = null;

        $this->patchJson('/api/collections/019fb4f0-0d71-7011-b678-0cb4004dc2a7', ['name' => 'Errands'])
            ->assertNotFound()
            ->assertJsonPath('message', 'That collection no longer exists. Refresh and try again.');
    }

    public function test_deleting_answers_204_and_a_missing_one_answers_404(): void
    {
        // 204 because there is nothing useful to say: the collection is gone, and the memos it
        // held are described by the memo list rather than by this route.
        $this->deleteJson('/api/collections/019fb4f0-0d71-7011-b678-0cb4004dc2a7')->assertNoContent();

        $this->assertSame(['019fb4f0-0d71-7011-b678-0cb4004dc2a7'], $this->repository->deleted);

        $this->repository->deleteResult = false;

        $this->deleteJson('/api/collections/019fb4f0-0d71-7011-b678-0cb4004dc2a7')->assertNotFound();
    }

    public function test_an_id_that_is_not_a_uuid_never_reaches_the_controller(): void
    {
        $this->patchJson('/api/collections/not-a-uuid', ['name' => 'Work'])->assertNotFound();
        $this->deleteJson('/api/collections/not-a-uuid')->assertNotFound();

        $this->assertSame([], $this->repository->renamed);
        $this->assertSame([], $this->repository->deleted);
    }

    /** @param list<string> $labels */
    private function collection(string $name, int $count, array $labels): Collection
    {
        return new Collection(
            id: '019fb4f0-0d71-7011-b678-0cb4004dc2a7',
            name: $name,
            memoCount: $count,
            recentLabels: $labels,
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }
}

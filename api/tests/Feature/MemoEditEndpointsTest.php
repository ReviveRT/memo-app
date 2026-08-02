<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Contracts\AudioStorage;
use App\Http\Requests\UpdateMemoRequest;
use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;
use Tests\Support\FakeMemoRepository;
use Tests\Support\RecordingAudioStorage;
use Tests\TestCase;

/**
 * Renaming a memo and deleting one.
 *
 * The two edits a client may make to a memo that already exists, kept apart from
 * MemoFilterEndpointsTest -- which owns filing and the list filters -- because they answer a
 * different question about the route. Filing is about *where* a memo is; these are about what
 * it is called and whether it is there at all.
 *
 * What is deliberately not tested here is that the SQL is right. The repository is faked, for
 * the reason FakeMemoRepository states: every statement in the real one is Postgres-specific
 * and a PHP imitation of it would be a second, wrong definition. What these pin is the HTTP
 * contract -- the status codes, the shapes, the validation, and the ordering between the row
 * and the blob.
 */
final class MemoEditEndpointsTest extends TestCase
{
    private const MEMO_ID = '019fb4ef-0d71-7011-b678-0cb4004dc2a7';

    private FakeMemoRepository $repository;

    private RecordingAudioStorage $storage;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);

        $this->storage = new RecordingAudioStorage;
        $this->app->instance(AudioStorage::class, $this->storage);
    }

    public function test_a_title_can_be_changed_and_answers_with_the_whole_memo(): void
    {
        $this->repository->renameResult = $this->memo(title: 'Shopping list');

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => 'Shopping list'])
            ->assertOk()
            // The whole memo rather than an acknowledgement, like every other write on this
            // resource, so the frontend can merge one shape by id.
            ->assertJsonPath('memo.id', self::MEMO_ID)
            ->assertJsonPath('memo.title', 'Shopping list');

        $this->assertSame([[self::MEMO_ID, 'Shopping list']], $this->repository->renamed);
    }

    public function test_a_title_is_trimmed_and_a_blank_one_clears_it(): void
    {
        $this->repository->renameResult = $this->memo(title: null);

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => '   '])
            ->assertOk()
            ->assertJsonPath('memo.title', null);

        // Null, not `''`. `coalesce(title, summary, left(transcript, 80), ...)` is how the
        // collection cards and reminder labels pick a label, and an empty string is not absent
        // to coalesce -- so a blank title stored as `''` renders as a blank card label instead
        // of falling back to the transcript.
        $this->assertSame([[self::MEMO_ID, null]], $this->repository->renamed);
    }

    public function test_an_oversized_or_null_byte_title_is_refused(): void
    {
        $this->patchJson('/api/memos/'.self::MEMO_ID, [
            'title' => str_repeat('a', UpdateMemoRequest::MAX_TITLE_LENGTH + 1),
        ])->assertStatus(422)->assertJsonValidationErrors('title');

        // Refused rather than truncated at the NUL, which is what libpq would do silently --
        // see App\Http\Rules\NoNullBytes for the measurement.
        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => "Sho\0pping"])
            ->assertStatus(422)
            ->assertJsonValidationErrors('title');

        $this->assertSame([], $this->repository->renamed);
    }

    public function test_renaming_a_memo_that_is_gone_is_a_404(): void
    {
        $this->repository->renameResult = null;

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => 'Anything'])
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo no longer exists. Refresh and try again.');
    }

    public function test_a_body_carrying_both_fields_applies_both(): void
    {
        $collection = '019fb4f0-0d71-7011-b678-0cb4004dc2a7';

        $this->repository->moveResult = $this->memo(title: 'Old', collectionId: $collection);
        $this->repository->renameResult = $this->memo(title: 'New', collectionId: $collection);

        $this->patchJson('/api/memos/'.self::MEMO_ID, [
            'collection_id' => $collection,
            'title' => 'New',
        ])
            ->assertOk()
            // The rename runs second, so its answer is the one returned. Both writes happened.
            ->assertJsonPath('memo.title', 'New')
            ->assertJsonPath('memo.collection_id', $collection);

        $this->assertSame([[self::MEMO_ID, $collection]], $this->repository->moved);
        $this->assertSame([[self::MEMO_ID, 'New']], $this->repository->renamed);
    }

    public function test_a_body_naming_only_a_title_does_not_touch_the_collection(): void
    {
        $this->repository->renameResult = $this->memo(title: 'New');

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => 'New'])->assertOk();

        // The field this route used to *require*. Sending it to mean "leave it alone" would be
        // a client asserting a value it cannot know is still current -- another tab may have
        // filed the memo since the list was loaded.
        $this->assertSame([], $this->repository->moved);
    }

    public function test_deleting_a_memo_answers_with_what_was_removed(): void
    {
        $this->repository->rows = [$this->memo(title: 'Shopping list')];

        $this->deleteJson('/api/memos/'.self::MEMO_ID)
            ->assertOk()
            ->assertJsonPath('memo.id', self::MEMO_ID)
            ->assertJsonPath('memo.title', 'Shopping list');

        $this->assertSame([self::MEMO_ID], $this->repository->deleted);
    }

    public function test_deleting_a_voice_memo_unlinks_its_recording(): void
    {
        $key = self::MEMO_ID.'.webm';

        $this->storage->put($key, 'not really audio');
        $this->repository->rows = [$this->memo(title: 'A recording')];
        $this->repository->audioPaths = [self::MEMO_ID => $key];

        $this->deleteJson('/api/memos/'.self::MEMO_ID)->assertOk();

        $this->assertFalse($this->storage->exists($key));
    }

    public function test_a_blob_that_will_not_delete_still_leaves_the_memo_deleted(): void
    {
        // The row is gone and the response says so, even though the volume refused the unlink.
        // The alternative is a 500 for a request that already succeeded, and a client that then
        // shows a memo the database no longer has. An orphan blob is reclaimable later; a
        // client and a database that disagree is not. MemoService has the full argument.
        $this->repository->rows = [$this->memo(title: 'A recording')];
        $this->repository->audioPaths = [self::MEMO_ID => 'never-written.webm'];

        $this->deleteJson('/api/memos/'.self::MEMO_ID)
            ->assertOk()
            ->assertJsonPath('memo.id', self::MEMO_ID);
    }

    public function test_deleting_a_memo_twice_is_a_404_the_second_time(): void
    {
        $this->repository->rows = [$this->memo(title: 'Shopping list')];

        $this->deleteJson('/api/memos/'.self::MEMO_ID)->assertOk();

        // Not idempotent in its status code, deliberately: the second request is a client
        // telling us about a memo it believes exists -- a second tab, or a stale list -- and it
        // should find out that it does not.
        $this->deleteJson('/api/memos/'.self::MEMO_ID)
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo no longer exists.');
    }

    public function test_a_delete_for_an_id_that_is_not_a_uuid_never_reaches_the_controller(): void
    {
        // whereUuid on the route. Without it the value reaches Postgres and comes back as a 500
        // from `invalid input syntax for type uuid`.
        $this->deleteJson('/api/memos/not-a-uuid')->assertNotFound();

        $this->assertSame([], $this->repository->deleted);
    }

    public function test_a_transcript_can_be_corrected(): void
    {
        // The case this exists for: a model produced the wrong words, and the person who spoke
        // them is the only one who can say so. UpdateMemoRequest has why the column stopped
        // being read-only.
        $corrected = 'Salut, mă numesc Vasile!';
        $this->repository->correctResult = $this->memo(title: null, transcript: $corrected);

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['transcript' => $corrected])
            ->assertOk()
            ->assertJsonPath('memo.transcript', $corrected);

        $this->assertSame([[self::MEMO_ID, $corrected]], $this->repository->corrected);
    }

    public function test_a_corrected_transcript_is_trimmed(): void
    {
        $this->repository->correctResult = $this->memo(title: null);

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['transcript' => "  Buy milk.  \n"])
            ->assertOk();

        $this->assertSame([[self::MEMO_ID, 'Buy milk.']], $this->repository->corrected);
    }

    public function test_a_blank_transcript_is_refused_rather_than_clearing_it(): void
    {
        // Unlike `title`, which may be cleared: a memo with no title falls back to the first
        // line of its transcript, while a memo with no transcript has no text at all -- it is
        // unfindable by search and indistinguishable from one whose recording produced nothing.
        // Somebody who wants that wants Delete.
        $this->patchJson('/api/memos/'.self::MEMO_ID, ['transcript' => '   '])
            ->assertStatus(422)
            ->assertJsonValidationErrors('transcript');

        $this->assertSame([], $this->repository->corrected);
    }

    public function test_an_oversized_or_null_byte_transcript_is_refused(): void
    {
        $this->patchJson('/api/memos/'.self::MEMO_ID, [
            'transcript' => str_repeat('a', UpdateMemoRequest::MAX_TRANSCRIPT_LENGTH + 1),
        ])->assertStatus(422)->assertJsonValidationErrors('transcript');

        // A NUL is silently destructive rather than fatal -- libpq truncates the bound
        // parameter at it, so this would store a one-character transcript for a long PATCH.
        $this->patchJson('/api/memos/'.self::MEMO_ID, ['transcript' => "a\0b"])
            ->assertStatus(422)
            ->assertJsonValidationErrors('transcript');

        $this->assertSame([], $this->repository->corrected);
    }

    public function test_correcting_a_memo_that_is_gone_is_a_404(): void
    {
        $this->repository->correctResult = null;

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['transcript' => 'Anything.'])
            ->assertNotFound();
    }

    public function test_a_title_and_a_transcript_in_one_body_both_apply(): void
    {
        // Two statements, one request. The transcript runs last, so the row that comes back
        // carries both edits -- see MemoController::update.
        $this->repository->renameResult = $this->memo(title: 'Introduction');
        $this->repository->correctResult = $this->memo(title: 'Introduction', transcript: 'Salut!');

        $this->patchJson('/api/memos/'.self::MEMO_ID, [
            'title' => 'Introduction',
            'transcript' => 'Salut!',
        ])
            ->assertOk()
            ->assertJsonPath('memo.title', 'Introduction')
            ->assertJsonPath('memo.transcript', 'Salut!');

        $this->assertSame([[self::MEMO_ID, 'Introduction']], $this->repository->renamed);
        $this->assertSame([[self::MEMO_ID, 'Salut!']], $this->repository->corrected);
    }

    public function test_a_body_naming_only_a_title_does_not_touch_the_transcript(): void
    {
        $this->repository->renameResult = $this->memo(title: 'Just a rename');

        $this->patchJson('/api/memos/'.self::MEMO_ID, ['title' => 'Just a rename'])->assertOk();

        $this->assertSame([], $this->repository->corrected);
    }

    /** A memo shaped like the row Postgres would have returned. */
    private function memo(
        ?string $title,
        ?string $collectionId = null,
        string $transcript = 'Buy milk, eggs and bread on the way home.',
    ): Memo {
        return new Memo(
            id: self::MEMO_ID,
            source: Memo::SOURCE_VOICE,
            status: 'ready',
            transcript: $transcript,
            title: $title,
            summary: null,
            tags: [],
            durationMs: 4200,
            lastError: null,
            lastErrorCode: null,
            createdAt: '2026-07-31T09:00:00.000Z',
            collectionId: $collectionId,
            reminders: [],
        );
    }
}

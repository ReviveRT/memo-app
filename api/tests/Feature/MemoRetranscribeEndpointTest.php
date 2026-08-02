<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;
use Tests\Support\FakeMemoRepository;
use Tests\TestCase;

/**
 * POST /api/memos/{id}/retranscribe -- decode a recording again, in a language the user names.
 *
 * Kept beside MemoRetryEndpointTest rather than inside it, mirroring the split in the
 * repository. Retry asks "this failed, try again" and its whole contract is which states it
 * refuses; this route is called about memos that *succeeded* -- a Romanian recording
 * transliterated into Cyrillic is a `ready` row with a transcript on it -- so the state it
 * mainly has to accept is the one Retry mainly has to refuse. One test file asserting both
 * would read as two contradictory contracts.
 *
 * Why the route exists at all is measured rather than assumed: nine language-ID approaches
 * across three architectures were run against one real 2.76-second Romanian memo and every one
 * of them answered Slavic or Baltic, two of them at 0.98 and 0.99 confidence. 005_memo_language.sql
 * has the table. Auto-detect stays the default; this is the override.
 *
 * The repository is faked, as everywhere in this suite. What only a live Postgres can show --
 * that `attempts` really is reset and `next_attempt_at` really is `now()` -- is MEMO-25's, and
 * neither is on the wire for an HTTP test to look at.
 */
final class MemoRetranscribeEndpointTest extends TestCase
{
    private const MEMO_ID = '019fc1de-b0a1-70fa-8b80-e13fa1b6de4c';

    private FakeMemoRepository $repository;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);
    }

    public function test_a_ready_memo_is_requeued_in_the_named_language(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertOk()
            ->assertJsonPath('memo.id', self::MEMO_ID)
            ->assertJsonPath('memo.language', 'ro')

            // `queued` is what the frontend acts on rather than merely displays: it flips
            // useMemoList's `pending` and restarts the poll that will show the new transcript
            // replacing the wrong one.
            ->assertJsonPath('memo.status', Memo::STATUS_QUEUED);
    }

    public function test_the_old_transcript_is_cleared_rather_than_left_in_place(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertOk()

            // Not cosmetic, and the one assertion here that is about the worker rather than
            // the client. `owed_audio` in memo_ai/pipeline.py decides whether a claimed memo
            // owes a transcript by asking whether it already has one -- so a requeued row
            // that kept the old text would be published straight back unchanged, and the
            // request would appear to succeed while changing nothing.
            ->assertJsonPath('memo.transcript', null);
    }

    public function test_the_title_cut_from_the_wrong_transcript_goes_too(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready')];

        // Regression, and it was found by running the endpoint rather than by reading it. The
        // title is cut out of the transcript, and `_FINISH_READY` ranks an existing title above
        // both of its fallbacks -- correctly, so a retry cannot downgrade a real title and so a
        // person can edit the column. Here that ordering preserved the wrong answer: the memo
        // came back with a Romanian transcript and still called `Салют`.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertOk()
            ->assertJsonPath('memo.title', null)
            ->assertJsonPath('memo.summary', null)
            ->assertJsonPath('memo.tags', []);
    }

    public function test_a_failed_memo_can_also_be_retranscribed(): void
    {
        // Both terminal states are accepted. A memo that failed *because* of the language --
        // no speech detected in audio the model was decoding as the wrong one -- is exactly
        // the case a plain Retry would send back to the same wrong answer.
        $this->repository->rows = [$this->memo(status: 'failed')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ru'])
            ->assertOk()
            ->assertJsonPath('memo.language', 'ru')
            ->assertJsonPath('memo.status', Memo::STATUS_QUEUED);
    }

    public function test_omitting_the_language_puts_the_memo_back_on_auto_detect(): void
    {
        // The way back for somebody who pinned the wrong language: null means detect, so this
        // is a real request rather than a malformed one.
        $this->repository->rows = [$this->memo(status: 'ready', language: 'lt')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', [])
            ->assertOk()
            ->assertJsonPath('memo.language', null);
    }

    public function test_an_empty_language_means_the_same_as_omitting_it(): void
    {
        // What a <select> whose "Auto-detect" option carries value="" actually sends.
        // ConvertEmptyStringsToNull turns it into a present null, which `nullable` has to
        // allow or the browser's default choice would be a 422.
        $this->repository->rows = [$this->memo(status: 'ready', language: 'lt')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => ''])
            ->assertOk()
            ->assertJsonPath('memo.language', null);
    }

    public function test_a_language_the_model_does_not_know_is_refused_before_the_memo_is_touched(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready')];

        // A locale rather than a language code is the likely mistake, and the one worth
        // refusing at the edge: accepted onto the row it becomes a memo that queues, gets
        // claimed, raises inside faster-whisper and lands in `failed` a poll interval later
        // with a library's message on it.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro-RO'])
            ->assertUnprocessable()
            ->assertJsonValidationErrors('language');

        $this->assertSame('ready', $this->repository->rows[0]->status);
    }

    public function test_a_typed_memo_cannot_be_transcribed_again(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready', source: Memo::SOURCE_TEXT)];

        // Permanent rather than transient, so it gets its own sentence: there is no audio to
        // decode and no amount of waiting produces any. Requeueing one would blank a
        // transcript the user typed themselves.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertStatus(409)
            ->assertSee('typed', escape: false);
    }

    public function test_a_memo_a_worker_is_holding_is_refused(): void
    {
        $this->repository->rows = [$this->memo(status: Memo::STATUS_PROCESSING)];

        // The transient refusal, and the reason it is a refusal rather than a queue: the
        // worker holding this row has a fence token in `locked_at`, and resetting the row
        // under it is what that column exists to prevent.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertStatus(409)
            ->assertSee('processing', escape: false);
    }

    public function test_retranscribing_a_memo_that_is_gone_is_a_404(): void
    {
        $this->repository->rows = [];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retranscribe', ['language' => 'ro'])
            ->assertNotFound();
    }

    public function test_an_id_that_is_not_a_uuid_never_reaches_the_controller(): void
    {
        $this->postJson('/api/memos/not-a-uuid/retranscribe', ['language' => 'ro'])
            ->assertNotFound();
    }

    /** A voice memo shaped like the row Postgres would have returned, in whichever state. */
    private function memo(
        string $status,
        string $source = Memo::SOURCE_VOICE,
        ?string $language = null,
    ): Memo {
        return new Memo(
            id: self::MEMO_ID,
            source: $source,
            status: $status,

            // A transcript is present, unlike the Retry fixture's, and that is the point of
            // this route: the job succeeded and produced the wrong words. Cyrillic, because
            // this is the real transcript the real recording produced.
            transcript: 'Салют, Манамеск Василий!',
            title: 'Салют',
            summary: null,
            tags: [],
            durationMs: 2760,
            lastError: null,
            lastErrorCode: null,
            createdAt: '2026-08-02T09:47:00.000Z',
            language: $language,
        );
    }
}

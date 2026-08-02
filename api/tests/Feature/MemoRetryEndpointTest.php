<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;
use Tests\Support\FakeMemoRepository;
use Tests\TestCase;

/**
 * POST /api/memos/{id}/retry -- the one route that is about the pipeline rather than the memo.
 *
 * Kept out of MemoEditEndpointsTest, which owns renaming and deleting, because this is not an
 * edit: nothing about what the memo *is* changes, and the interesting behaviour is entirely in
 * which states the route refuses. That refusal is the whole of the contract worth pinning here
 * -- MemoRepository::requeue has the argument for why requeueing a `processing` row would let
 * two workers transcribe one memo, and none of these tests can show that, because the fake has
 * no worker. What they show is that the route never gives such a row the chance.
 *
 * The repository is faked, as everywhere in this suite: `php artisan test` runs on sqlite in
 * memory and the real statement is Postgres. The half that only a live database can show --
 * that `attempts` really is reset, that `next_attempt_at` really is `now()` -- is MEMO-25's,
 * and neither value is on the wire for an HTTP test to look at anyway.
 */
final class MemoRetryEndpointTest extends TestCase
{
    private const MEMO_ID = '019fb4ef-0d71-7011-b678-0cb4004dc2a7';

    private FakeMemoRepository $repository;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);
    }

    public function test_a_failed_memo_goes_back_to_the_queue_and_answers_with_the_whole_memo(): void
    {
        $this->repository->rows = [$this->memo(status: 'failed')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertOk()
            ->assertJsonPath('memo.id', self::MEMO_ID)

            // The status is the part the frontend acts on rather than merely displays: a
            // `queued` row is non-terminal, which flips useMemoList's `pending` and restarts
            // the poll that will show the retry finishing. A 204 would leave the card on
            // `failed` until something else refreshed it.
            ->assertJsonPath('memo.status', Memo::STATUS_QUEUED);
    }

    public function test_the_reason_stays_on_the_row_while_it_waits(): void
    {
        $this->repository->rows = [$this->memo(status: 'failed')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertOk()

            // Not cleared, and the frontend does not need it to be: `failureReason` in
            // web/src/memoFailure.js gates on `status === 'failed'`, so the sentence stops
            // being rendered the moment this response lands. The column is the *last* error,
            // and the write that knows it is over is the next successful transcription --
            // `_COMMIT_TRANSCRIPT` in memo_ai/memos.py. The worker's own retry path writes
            // `last_error` onto a `queued` row for the same reason.
            ->assertJsonPath('memo.last_error', 'The local transcription model could not be loaded.')

            // And its code, which travels with it. The two are written by one statement and
            // read by two different readers -- a person, and the branch in memoFailure.js
            // that decides whether a failed memo is kept at all.
            ->assertJsonPath('memo.last_error_code', 'provider_unavailable');
    }

    public function test_retrying_a_ready_memo_is_refused(): void
    {
        $this->repository->rows = [$this->memo(status: 'ready')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertStatus(409)

            // Named states rather than "cannot be retried", because the frontend renders this
            // sentence to the user verbatim and "why did nothing happen" is the only question
            // they have. A 404 here would answer it with something false.
            ->assertJsonPath(
                'message',
                'Only a failed memo can be retried, and this one is ready.'
                    .' Refresh to see where it got to.',
            );
    }

    public function test_retrying_a_memo_a_worker_is_holding_is_refused(): void
    {
        // The dangerous one. `processing` means a live claim, and the worker's writes are
        // fenced on `locked_at` rather than on the status -- so a row put back to `queued`
        // underneath its owner is claimable by the other replica while the first is still
        // transcribing it. See MemoRepository::requeue.
        $this->repository->rows = [$this->memo(status: Memo::STATUS_PROCESSING)];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertStatus(409)
            ->assertJsonPath(
                'message',
                'Only a failed memo can be retried, and this one is processing.'
                    .' Refresh to see where it got to.',
            );
    }

    public function test_pressing_retry_twice_is_a_409_the_second_time(): void
    {
        $this->repository->rows = [$this->memo(status: 'failed')];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')->assertOk();

        // Reachable by a double click and by a second tab, not only by a broken client. The
        // second press is refused rather than shrugged off, because "it is already queued" is
        // the answer to the question being asked and a second 200 would not be.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertStatus(409)
            ->assertJsonPath(
                'message',
                'Only a failed memo can be retried, and this one is queued.'
                    .' Refresh to see where it got to.',
            );
    }

    public function test_retrying_a_memo_that_is_gone_is_a_404(): void
    {
        $this->repository->rows = [];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry')
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo no longer exists.');
    }

    public function test_an_id_that_is_not_a_uuid_never_reaches_the_controller(): void
    {
        // whereUuid on the route, as on every other id in api/routes/api.php. Without it the
        // value reaches Postgres and comes back as a 500 from `invalid input syntax for type
        // uuid` -- a bad request answered with a server error and a stack trace on stderr.
        $this->postJson('/api/memos/not-a-uuid/retry')->assertNotFound();
    }

    public function test_a_body_is_neither_required_nor_read(): void
    {
        $this->repository->rows = [$this->memo(status: 'failed')];

        // No FormRequest on this route: the id is in the path and the new state is not the
        // client's to choose. A body carrying a status is ignored rather than honoured, which
        // is the property that keeps `ready` out of a client's reach.
        $this->postJson('/api/memos/'.self::MEMO_ID.'/retry', ['status' => 'ready'])
            ->assertOk()
            ->assertJsonPath('memo.status', Memo::STATUS_QUEUED);
    }

    /** A voice memo shaped like the row Postgres would have returned, in whichever state. */
    private function memo(string $status): Memo
    {
        return new Memo(
            id: self::MEMO_ID,
            source: Memo::SOURCE_VOICE,
            status: $status,

            // No transcript, which is what `failed` means on this table: the terminal failure
            // write is reachable only from a transcription that produced nothing. See
            // memo_ai/memos.py.
            transcript: null,
            title: null,
            summary: null,
            tags: [],
            durationMs: 4200,

            // A failure the recording's owner cannot fix by retrying and the app does not
            // keep -- `provider_unavailable` is the opposite kind, and the one this route
            // exists for. It is here rather than `no_speech` so this fixture is a memo that
            // genuinely wants a Retry button; web/src/memoFailure.js is where the two kinds
            // part company.
            lastError: 'The local transcription model could not be loaded.',
            lastErrorCode: 'provider_unavailable',
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }
}

<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Http\Requests\StoreReminderRequest;
use App\Repositories\MemoRepository;
use App\Repositories\ReminderRepository;
use App\Services\Memos\Memo;
use App\Services\Memos\Reminder;
use Tests\Support\FakeMemoRepository;
use Tests\Support\FakeReminderRepository;
use Tests\TestCase;

/**
 * The four reminder routes, and the two contracts they exist to keep.
 *
 * **First: every write answers with the memo, not the reminder.** The frontend holds its memos
 * keyed by id and reconciles them against whatever the API last said, so a response carrying
 * the memo is a row it can write directly -- where a bare reminder would make it find the
 * memo, splice the reminder in, and reproduce the soonest-first ordering client-side.
 *
 * **Second: the index is the exception, and answers reminders.** It feeds the browser's
 * delivery loop, which has to know about a reminder on a memo that is nowhere on screen -- the
 * fast strip holds only unfiled memos, so without it a reminder set and then filed into a
 * collection would silently never fire.
 */
final class ReminderEndpointsTest extends TestCase
{
    private const MEMO_ID = '019fb4ef-0d71-7011-b678-0cb4004dc2a7';

    private const REMINDER_ID = '019fb500-0d71-7011-b678-0cb4004dc2a7';

    private FakeReminderRepository $reminders;

    private FakeMemoRepository $memos;

    protected function setUp(): void
    {
        parent::setUp();

        $this->reminders = new FakeReminderRepository;
        $this->app->instance(ReminderRepository::class, $this->reminders);

        $this->memos = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->memos);
    }

    public function test_setting_a_reminder_answers_201_with_the_memo_carrying_it(): void
    {
        $this->memos->rows = [$this->memo([
            new Reminder(self::REMINDER_ID, '2026-08-02T09:00:00.000Z', 'ring the dentist', null),
        ])];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => '2099-08-02T09:00:00Z',
            'note' => 'ring the dentist',
        ])
            ->assertCreated()
            ->assertJsonPath('memo.id', self::MEMO_ID)
            ->assertJsonPath('memo.reminders.0.note', 'ring the dentist')
            // Null while a reminder is still owed. It is the whole of "has this fired yet?" on
            // the client, so it has to reach the response as null rather than be dropped.
            ->assertJsonPath('memo.reminders.0.delivered_at', null);
    }

    public function test_the_instant_reaches_the_repository_normalised_to_utc(): void
    {
        // Both of the card's controls -- an alarm at a wall-clock time and a timer some
        // minutes out -- resolve to one absolute instant in the browser, so the API has one
        // field and no idea which was used.
        $this->memos->rows = [$this->memo([])];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => '2099-08-02T09:00:00+02:00',
        ])->assertCreated();

        $this->assertSame('2099-08-02T07:00:00.000000+00:00', $this->reminders->inserted[0][2]);
        $this->assertNull($this->reminders->inserted[0][3]);
    }

    public function test_a_reminder_in_the_past_is_refused_in_words_a_reader_can_act_on(): void
    {
        $response = $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => '2020-01-01T09:00:00Z',
        ]);

        $response->assertStatus(422)->assertJsonValidationErrors('remind_at');

        // Named, so the message describes the control rather than a JSON key.
        $this->assertStringContainsString('reminder time', (string) $response->json('message'));
        $this->assertSame([], $this->reminders->inserted);
    }

    public function test_a_reminder_a_few_seconds_ago_is_allowed_for_clock_skew(): void
    {
        // The instant is computed by the browser and judged by the API, so a short timer is a
        // race against whatever the two clocks disagree by. Without the tolerance, "in 60
        // seconds" from a browser slightly behind the server is refused, which reads as the
        // timer button being broken.
        $this->memos->rows = [$this->memo([])];

        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => gmdate('Y-m-d\TH:i:s\Z', time() - (StoreReminderRequest::PAST_TOLERANCE_SECONDS - 10)),
        ])->assertCreated();
    }

    public function test_an_oversized_or_null_byte_note_is_rejected(): void
    {
        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => '2099-08-02T09:00:00Z',
            'note' => str_repeat('a', StoreReminderRequest::MAX_NOTE_LENGTH + 1),
        ])->assertStatus(422)->assertJsonValidationErrors('note');

        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', [
            'remind_at' => '2099-08-02T09:00:00Z',
            'note' => "ring\0the dentist",
        ])->assertStatus(422)->assertJsonValidationErrors('note');

        $this->assertSame([], $this->reminders->inserted);
    }

    public function test_a_reminder_for_a_memo_that_does_not_exist_is_a_404(): void
    {
        // Caught from the foreign key rather than checked with a SELECT first, because a
        // SELECT would only race the INSERT and land back here anyway.
        $this->reminders->memoExists = false;

        $this->postJson('/api/memos/'.self::MEMO_ID.'/reminders', ['remind_at' => '2099-08-02T09:00:00Z'])
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo no longer exists. Refresh and try again.');
    }

    public function test_acknowledging_answers_with_the_memo_and_takes_no_body(): void
    {
        // The delivery time is now() in SQL rather than anything the client sends: a browser's
        // clock has no business writing the column used to judge whether reminders arrive late.
        $this->reminders->memoId = self::MEMO_ID;
        $this->memos->rows = [$this->memo([
            new Reminder(self::REMINDER_ID, '2026-08-02T09:00:00.000Z', null, '2026-08-02T09:00:04.000Z'),
        ])];

        $this->patchJson('/api/reminders/'.self::REMINDER_ID)
            ->assertOk()
            ->assertJsonPath('memo.reminders.0.delivered_at', '2026-08-02T09:00:04.000Z');

        $this->assertSame([self::REMINDER_ID], $this->reminders->delivered);
    }

    public function test_deleting_answers_with_the_memo_without_it(): void
    {
        // 200 with the memo rather than 204, unlike deleting a collection: the memo still
        // exists and its `reminders` array has changed, so there is something to describe.
        $this->reminders->memoId = self::MEMO_ID;
        $this->memos->rows = [$this->memo([])];

        $this->deleteJson('/api/reminders/'.self::REMINDER_ID)
            ->assertOk()
            ->assertJsonPath('memo.reminders', []);

        $this->assertSame([self::REMINDER_ID], $this->reminders->removed);
    }

    public function test_acknowledging_or_deleting_a_missing_reminder_is_a_404(): void
    {
        $this->reminders->memoId = null;

        $this->patchJson('/api/reminders/'.self::REMINDER_ID)->assertNotFound();
        $this->deleteJson('/api/reminders/'.self::REMINDER_ID)->assertNotFound();
    }

    public function test_the_pending_list_carries_a_label_rather_than_the_memo(): void
    {
        // A notification body shows eighty characters, and the transcript is the largest thing
        // on the row -- so this route carries what a notification needs to name the memo and
        // nothing else.
        $this->reminders->pendingRows = [
            (object) [
                'id' => self::REMINDER_ID,
                'memo_id' => self::MEMO_ID,
                'memo_label' => 'Dentist Appointment',
                'remind_at_iso' => '2026-08-02T09:00:00.000Z',
                'note' => 'ring the dentist',
            ],
        ];

        $this->getJson('/api/reminders')
            ->assertOk()
            ->assertJsonPath('reminders.0.memo_label', 'Dentist Appointment')
            ->assertJsonPath('reminders.0.memo_id', self::MEMO_ID)
            ->assertJsonPath('reminders.0.remind_at', '2026-08-02T09:00:00.000Z')
            ->assertJsonPath('reminders.0.note', 'ring the dentist')
            // No transcript, and no `delivered_at`: everything here is by definition
            // undelivered, so a key that would be null on every row is not sent.
            ->assertJsonMissingPath('reminders.0.transcript')
            ->assertJsonMissingPath('reminders.0.delivered_at');
    }

    public function test_the_pending_list_is_never_cached(): void
    {
        // It is polled, and a cached answer is a reminder that fires late or twice.
        $response = $this->getJson('/api/reminders');

        $this->assertStringContainsString('no-store', (string) $response->headers->get('Cache-Control'));
    }

    /** @param list<Reminder> $reminders */
    private function memo(array $reminders): Memo
    {
        return new Memo(
            id: self::MEMO_ID,
            source: 'text',
            status: 'ready',
            transcript: 'Call the dentist on Tuesday',
            title: 'Dentist',
            summary: null,
            tags: [],
            durationMs: null,
            lastError: null,
            createdAt: '2026-07-31T09:00:00.000Z',
            collectionId: null,
            reminders: $reminders,
        );
    }
}

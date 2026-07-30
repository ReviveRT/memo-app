<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Services\Memos\Memo;
use App\Services\Memos\MemoService;
use PHPUnit\Framework\TestCase;
use Tests\Support\FakeMemoRepository;

/** The id, and what a text memo is written as. No framework, no database. */
final class MemoServiceTest extends TestCase
{
    public function test_a_text_memo_is_queued_with_the_typed_text_as_its_transcript(): void
    {
        $repository = new FakeMemoRepository;

        $memo = (new MemoService($repository))->createFromText('Call the dentist');

        $this->assertSame(Memo::SOURCE_TEXT, $memo->source);
        $this->assertSame(Memo::STATUS_QUEUED, $memo->status);
        $this->assertSame('Call the dentist', $memo->transcript);
        $this->assertSame([$memo->id], array_column($repository->inserted, 'id'));
    }

    public function test_the_generated_id_is_a_uuid_v7(): void
    {
        $id = (new MemoService(new FakeMemoRepository))->createFromText('x')->id;

        $this->assertMatchesRegularExpression(
            '/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/',
            $id,
            'The version nibble must be 7 and the variant must be RFC 4122.',
        );
    }

    public function test_ids_minted_in_sequence_sort_in_that_order(): void
    {
        // The property 001_init.sql relies on when it refuses to default `id` to
        // gen_random_uuid(): v7 leads with a millisecond timestamp, so lexical order
        // is insertion order and the primary key agrees with created_at. v4 would
        // scatter. Fifty in one loop lands inside a single millisecond, which is the
        // case worth pinning -- the timestamp alone does not order those, and this
        // passes because the generator carries a counter within the millisecond too.
        $service = new MemoService(new FakeMemoRepository);

        $ids = [];

        for ($i = 0; $i < 50; $i++) {
            $ids[] = $service->createFromText("memo {$i}")->id;
        }

        $sorted = $ids;
        sort($sorted, SORT_STRING);

        $this->assertSame($sorted, $ids);
    }
}

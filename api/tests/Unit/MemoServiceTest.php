<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Exceptions\StorageException;
use App\Services\Memos\AudioUpload;
use App\Services\Memos\Memo;
use App\Services\Memos\MemoService;
use PHPUnit\Framework\TestCase;
use Tests\Support\FakeMemoRepository;
use Tests\Support\RecordingAudioStorage;

/** The id, and what each kind of memo is written as. No framework, no database. */
final class MemoServiceTest extends TestCase
{
    private FakeMemoRepository $repository;

    private RecordingAudioStorage $storage;

    private MemoService $service;

    /** Files written by a test, removed afterwards. @var list<string> */
    private array $temporaryFiles = [];

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->storage = new RecordingAudioStorage;
        $this->service = new MemoService($this->repository, $this->storage);
    }

    protected function tearDown(): void
    {
        foreach ($this->temporaryFiles as $path) {
            @unlink($path);
        }

        parent::tearDown();
    }

    public function test_a_text_memo_is_queued_with_the_typed_text_as_its_transcript(): void
    {
        $memo = $this->service->createFromText('Call the dentist');

        $this->assertSame(Memo::SOURCE_TEXT, $memo->source);
        $this->assertSame(Memo::STATUS_QUEUED, $memo->status);
        $this->assertSame('Call the dentist', $memo->transcript);
        $this->assertSame([$memo->id], array_column($this->repository->inserted, 'id'));
    }

    public function test_a_text_memo_carries_no_audio_columns(): void
    {
        $this->service->createFromText('Call the dentist');

        // Both null, so `audio_path IS NULL` stays a true statement about every text
        // memo -- which is what MEMO-23 branches on to decide whether a row has
        // anything to play. Nothing writes the pair for a typed memo, and the point of
        // asserting it is that the repository now takes the two as defaulted
        // parameters, where a stray argument would go unnoticed.
        $this->assertNull($this->repository->inserted[0]['audio_path']);
        $this->assertNull($this->repository->inserted[0]['audio_mime']);
        $this->assertSame([], $this->storage->written);
    }

    public function test_a_voice_memo_is_queued_with_no_transcript_and_the_key_it_was_stored_under(): void
    {
        $memo = $this->service->createFromAudio($this->upload('OggS fake bytes', 'audio/ogg', 'ogg'));

        $this->assertSame(Memo::SOURCE_VOICE, $memo->source);
        $this->assertSame(Memo::STATUS_QUEUED, $memo->status);

        // The null transcript is the entire signal that this row owes a transcription:
        // memo_ai/pipeline.py branches on `transcript IS NULL` and on nothing else.
        $this->assertNull($memo->transcript);

        $insert = $this->repository->inserted[0];

        $this->assertSame("{$memo->id}.ogg", $insert['audio_path']);
        $this->assertSame('audio/ogg', $insert['audio_mime']);

        // The contract with the worker, stated as one assertion: the key on the row is
        // the key the bytes are under. The worker resolves it against its own AUDIO_DIR
        // (memo_ai/pipeline.py::audio_file), so a row and a blob that disagree here is a
        // memo that fails transcription for a reason nothing in either log explains.
        $this->assertSame('OggS fake bytes', $this->storage->blobs[$insert['audio_path']]);
    }

    public function test_the_audio_key_is_relative_and_extends_the_id(): void
    {
        $memo = $this->service->createFromAudio($this->upload('bytes', 'video/webm', 'webm'));

        $key = (string) $this->repository->inserted[0]['audio_path'];

        // Relative, because AudioStorage addresses blobs by key and refuses an absolute
        // one; and derived from the id, because that is what lets a blob on the volume
        // be traced back to its row when something has gone wrong.
        $this->assertSame("{$memo->id}.webm", $key);
        $this->assertStringStartsNotWith('/', $key);
    }

    public function test_the_blob_is_written_before_the_row_that_points_at_it(): void
    {
        // The ordering the worker depends on. Both ai-worker replicas poll roughly once
        // a second and open whatever `audio_path` names, so a row inserted ahead of its
        // file is a claim that can be acted on before it is true. Pinned by making the
        // write fail: if the INSERT went first it would already be recorded here.
        $this->storage->failOnPut = true;

        try {
            $this->service->createFromAudio($this->upload('bytes', 'video/webm', 'webm'));
            $this->fail('A failed blob write must not be swallowed.');
        } catch (StorageException) {
            // Expected: an unwritable volume is the operator's 500, not the user's 4xx.
        }

        $this->assertSame([], $this->repository->inserted, 'No row may exist without its audio.');
    }

    public function test_each_recording_gets_its_own_key(): void
    {
        $first = $this->service->createFromAudio($this->upload('one', 'video/webm', 'webm'));
        $second = $this->service->createFromAudio($this->upload('two', 'video/webm', 'webm'));

        // Two recordings a second apart are indistinguishable to a browser -- no
        // client-supplied name reaches the key -- so the id is the whole of what keeps
        // the second from overwriting the first. Worth pinning because the failure is
        // silent: the blob would be replaced and both rows would transcribe to the same
        // words.
        $this->assertNotSame($first->id, $second->id);
        $this->assertCount(2, $this->storage->written);
        $this->assertSame('one', $this->storage->blobs["{$first->id}.webm"]);
        $this->assertSame('two', $this->storage->blobs["{$second->id}.webm"]);
    }

    public function test_the_generated_id_is_a_uuid_v7(): void
    {
        $id = $this->service->createFromText('x')->id;

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
        $ids = [];

        for ($i = 0; $i < 50; $i++) {
            $ids[] = $this->service->createFromText("memo {$i}")->id;
        }

        $sorted = $ids;
        sort($sorted, SORT_STRING);

        $this->assertSame($sorted, $ids);
    }

    /**
     * An AudioUpload over a real file, because AudioStorage reads the path it is given.
     */
    private function upload(string $contents, string $mimeType, string $extension): AudioUpload
    {
        $path = tempnam(sys_get_temp_dir(), 'memo-upload-');

        $this->assertIsString($path);
        file_put_contents($path, $contents);
        $this->temporaryFiles[] = $path;

        return new AudioUpload(path: $path, mimeType: $mimeType, extension: $extension);
    }
}

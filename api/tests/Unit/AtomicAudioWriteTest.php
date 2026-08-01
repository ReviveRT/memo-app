<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Exceptions\StorageException;
use App\Storage\LocalAudioStorage;
use Tests\TestCase;

/**
 * The write-ordering half of MEMO-11, from the storage side.
 *
 * The ordering *between* the blob and the row is MemoServiceTest's
 * (test_the_blob_is_written_before_the_row_that_points_at_it). This is the half below
 * that: whatever appears under a key must be the whole file or nothing, because the
 * Python worker claims a queued row about once a second and opens `audio_path`
 * unconditionally -- and a truncated container is the one failure a reader cannot
 * detect. It decodes to whatever was written and transcribes it.
 *
 * **What cannot be tested here, stated rather than implied.** The fsync before the
 * rename is there for a host losing power between the two, and nothing in a test
 * process can cut power. What is checked is everything either side of it: that the
 * whole file arrives, that a write which fails leaves the key absent, and that no
 * partial file is left lying around under any name. `.part` siblings are the shape a
 * failure would take here, and an orphaned one is not merely untidy -- MEMO-11 accepts
 * orphan blobs on the understanding that a sweep can reclaim them, which is only true
 * of files under a key it can recognise.
 */
final class AtomicAudioWriteTest extends TestCase
{
    private string $root;

    protected function setUp(): void
    {
        parent::setUp();

        $this->root = sys_get_temp_dir().'/memo-atomic-'.bin2hex(random_bytes(6));
        mkdir($this->root, 0775);
    }

    protected function tearDown(): void
    {
        // rmdir as well as unlink: one test puts a *directory* where a key should go, and
        // unlink alone leaves it behind -- which then leaves the root behind too, since
        // rmdir refuses a non-empty directory. A suite that quietly accumulates a temp
        // tree per run is the kind of thing nobody notices until a disk fills.
        foreach (glob($this->root.'/*') ?: [] as $path) {
            is_dir($path) ? @rmdir($path) : @unlink($path);
        }

        @rmdir($this->root);

        parent::tearDown();
    }

    public function test_a_recording_larger_than_one_buffer_arrives_whole(): void
    {
        // 1 MiB of noise, which is several times any stream_copy_to_stream chunk, so a
        // copy that stopped at a buffer boundary would be visible here. Compared by hash
        // rather than by size: a short copy and a copy of the wrong bytes are different
        // bugs and only one of them changes the length.
        $contents = random_bytes(1024 * 1024);
        $source = $this->root.'/source.bin';
        file_put_contents($source, $contents);

        $storage = new LocalAudioStorage($this->root);
        $storage->putFile('memo.webm', $source);

        $this->assertSame(hash('sha256', $contents), hash_file('sha256', $this->root.'/memo.webm'));
        $this->assertSame(strlen($contents), $storage->size('memo.webm'));
    }

    public function test_an_unreadable_source_fails_before_anything_is_created(): void
    {
        $storage = new LocalAudioStorage($this->root);

        // A source that cannot be opened, which is what an upload temp file already
        // unlinked by PHP looks like. The failure has to reach the caller: MemoService
        // inserts the row on the next line, and a swallowed write would give the worker a
        // memo pointing at nothing.
        try {
            $storage->putFile('memo.webm', $this->root.'/not-here.bin');
            $this->fail('An unreadable source must not be stored silently.');
        } catch (StorageException) {
            $this->assertFalse($storage->exists('memo.webm'));
            $this->assertSame([], $this->leftovers());
        }
    }

    public function test_a_write_that_fails_after_the_temp_file_exists_cleans_it_up(): void
    {
        $storage = new LocalAudioStorage($this->root);

        // This is the case the cleanup in LocalAudioStorage::write() exists for, and the
        // test above does *not* reach it: an unreadable source fails at the fopen of the
        // source, before any temp file has been created, so asserting "no leftovers"
        // there is true whether the catch block works or not. Verified by probing both --
        // this one creates a `.part` and the other never does.
        //
        // A rename that cannot succeed is the cheapest way to fail *after* the bytes are
        // written and fsynced: rename(2) refuses to replace a directory with a file. What
        // matters is that the failure is not a partial file left under a real key, which
        // is the one thing a reader cannot detect.
        mkdir($this->root.'/memo.webm');

        try {
            $storage->put('memo.webm', 'hello');
            $this->fail('A rename that cannot succeed must not be reported as a stored blob.');
        } catch (StorageException) {
            $this->assertSame([], glob($this->root.'/*.part') ?: [], 'The temp file must not survive.');
            $this->assertSame(['memo.webm'], $this->leftovers());
        }
    }

    public function test_a_successful_write_leaves_nothing_beside_the_key(): void
    {
        $storage = new LocalAudioStorage($this->root);

        $storage->put('one.webm', 'first');
        $storage->put('two.webm', 'second');

        // The temp file is renamed into place rather than copied, so each write leaves
        // exactly one file. A `.part` surviving here would mean the rename silently did
        // not happen and the key's contents came from somewhere else.
        $this->assertSame(['one.webm', 'two.webm'], $this->leftovers());
    }

    /** Everything in the root, sorted, so an unexpected file is named in the failure. */
    private function leftovers(): array
    {
        $names = array_map('basename', glob($this->root.'/*') ?: []);
        sort($names);

        return $names;
    }
}

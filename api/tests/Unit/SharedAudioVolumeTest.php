<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Storage\LocalAudioStorage;
use Tests\TestCase;

/**
 * The permission half of MEMO-12, from the PHP side.
 *
 * What this suite cannot do is the acceptance criterion itself -- write from the
 * api container and delete from the worker container -- because that needs two
 * containers and a Docker volume. What it can do is pin the modes that make the
 * cross-container part work, and those are exactly the part that broke silently:
 * mkdir()'s mode argument is masked by the umask, so DIRECTORY_MODE was a request
 * the filesystem quietly declined and every directory the API created came out
 * group-readable but not group-writable. A worker sharing only the `memo` group
 * could then read every blob and unlink none of them.
 *
 * Each test sets the umask itself rather than trusting the one it inherits. The
 * bug only appears under a umask that masks group write, and a suite that happens
 * to run under 0002 would pass against the broken code.
 */
final class SharedAudioVolumeTest extends TestCase
{
    private string $root;

    private int $umask;

    protected function setUp(): void
    {
        parent::setUp();

        $this->umask = umask(0022);
        $this->root = sys_get_temp_dir().'/memo-audio-'.bin2hex(random_bytes(6));
        mkdir($this->root, 0775);
    }

    protected function tearDown(): void
    {
        umask($this->umask);
        $this->deleteTree($this->root);

        parent::tearDown();
    }

    public function test_directories_created_for_a_key_are_group_writable_and_setgid(): void
    {
        // The failure this pins: under umask 0022 these arrived as 2755
        // (drwxr-sr-x). Group write is what unlink(2) checks -- on the directory,
        // not on the file -- so without it the worker cannot delete a blob it can
        // read. Setgid keeps the group correct for anything created deeper down.
        // Three levels rather than one, so the walk that applies the mode runs more
        // than once and a key layout deeper than today's is covered too.
        $storage = new LocalAudioStorage($this->root);

        $storage->put('2026/07/15/memo.webm', 'audio');

        $this->assertSame('2775', $this->mode("{$this->root}/2026"));
        $this->assertSame('2775', $this->mode("{$this->root}/2026/07"));
        $this->assertSame('2775', $this->mode("{$this->root}/2026/07/15"));
    }

    public function test_blobs_are_group_readable_and_writable(): void
    {
        // 0664, not 0644: the worker deletes the file, and a group that cannot
        // write it cannot be trusted to truncate or replace it either. Set by an
        // explicit chmod before the rename, for the same reason as the directories.
        $storage = new LocalAudioStorage($this->root);

        $storage->put('flat.webm', 'audio');

        $this->assertSame('0664', $this->mode("{$this->root}/flat.webm"));
    }

    public function test_the_root_is_left_alone(): void
    {
        // The root is the volume mount point. Its mode is seeded from the image at
        // volume-creation time (api/Dockerfile) and belongs to whoever created the
        // volume -- so an upload must not quietly widen it. A key with no directory
        // component is the case that would: dirname() of it *is* the root.
        chmod($this->root, 0700);

        $storage = new LocalAudioStorage($this->root);
        $storage->put('flat.webm', 'audio');

        $this->assertSame('0700', $this->mode($this->root));
    }

    public function test_an_existing_directory_is_reused_without_a_second_mkdir(): void
    {
        // ensureDirectory() returns early when the directory is already there, which
        // is also what keeps two concurrent uploads from turning a lost mkdir race
        // into a failed request. Two writes under one prefix is the cheap proof that
        // the early return does not break the second one.
        $storage = new LocalAudioStorage($this->root);

        $storage->put('2026/07/first.webm', 'one');
        $storage->put('2026/07/second.webm', 'two');

        $this->assertSame('2775', $this->mode("{$this->root}/2026/07"));
        $this->assertTrue($storage->exists('2026/07/first.webm'));
        $this->assertTrue($storage->exists('2026/07/second.webm'));
    }

    /** Four octal digits, so the setgid bit is visible rather than truncated away. */
    private function mode(string $path): string
    {
        clearstatcache(true, $path);

        return substr(sprintf('%04o', fileperms($path)), -4);
    }

    private function deleteTree(string $path): void
    {
        if (! is_dir($path)) {
            return;
        }

        // Restore a mode the walk can actually traverse: test_the_root_is_left_alone
        // narrows it deliberately.
        @chmod($path, 0775);

        foreach (array_diff(scandir($path) ?: [], ['.', '..']) as $entry) {
            $child = "{$path}/{$entry}";

            is_dir($child) ? $this->deleteTree($child) : @unlink($child);
        }

        @rmdir($path);
    }
}

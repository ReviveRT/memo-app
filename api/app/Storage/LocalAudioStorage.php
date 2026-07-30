<?php

declare(strict_types=1);

namespace App\Storage;

use App\Contracts\AudioStorage;
use App\Exceptions\StorageException;
use Throwable;

/**
 * AudioStorage backed by AUDIO_DIR on the shared `audio` Docker volume, which the
 * Python worker mounts at the same path and reads by key.
 */
final class LocalAudioStorage implements AudioStorage
{
    /** Group-writable: the API writes these files and the worker deletes them (MEMO-12). */
    private const FILE_MODE = 0664;

    /**
     * Group-writable, and setgid.
     *
     * Group-writable because unlink(2) checks the *directory*, not the file: the
     * worker deletes what this class writes, and 0664 on the blob buys nothing if
     * the directory holding it is 0755. Setgid because that is what keeps the group
     * correct all the way down -- Linux inherits it onto every new subdirectory, so a
     * container running as a different uid with `memo` as a supplementary group
     * still lands its files in the shared group.
     */
    private const DIRECTORY_MODE = 02775;

    private readonly string $root;

    public function __construct(string $root)
    {
        $this->root = rtrim($root, '/');
    }

    public function put(string $key, string $contents): void
    {
        $this->write($key, static function ($handle) use ($contents): void {
            if (fwrite($handle, $contents) !== strlen($contents)) {
                throw new StorageException('Short write.');
            }
        });
    }

    public function putFile(string $key, string $sourcePath): void
    {
        $source = @fopen($sourcePath, 'rb');

        if ($source === false) {
            throw new StorageException("Cannot read source file {$sourcePath}.");
        }

        try {
            $this->write($key, static function ($handle) use ($source): void {
                // Only a regular file has a meaningful size to compare against. A
                // FIFO or a socket reports size 0 while copying any number of
                // bytes, so comparing unconditionally would reject every
                // non-regular source. S_IFMT / S_IFREG, since PHP exposes no
                // constant for the test.
                $stat = fstat($source);
                $isRegularFile = is_array($stat) && (($stat['mode'] ?? 0) & 0170000) === 0100000;
                $expected = $isRegularFile ? (int) $stat['size'] : null;

                $copied = stream_copy_to_stream($source, $handle);

                if ($copied === false) {
                    throw new StorageException('Copy from source file failed.');
                }

                // Belt and braces over the returned length. Verified on this image
                // that a destination hitting ENOSPC makes the call above return
                // false outright -- but that is an implementation detail, not the
                // contract: the documented return is "the total number of bytes
                // copied", which a partial copy also satisfies. If it ever returns
                // short instead of false, the alternative is a truncated file
                // fsynced and renamed into place under a real key, which is the one
                // outcome this write path exists to prevent.
                if ($expected !== null && $copied !== $expected) {
                    throw new StorageException("Short copy: {$copied} of {$expected} bytes.");
                }
            });
        } finally {
            fclose($source);
        }
    }

    public function exists(string $key): bool
    {
        return is_file($this->path($key));
    }

    public function size(string $key): ?int
    {
        $path = $this->path($key);

        if (! is_file($path)) {
            return null;
        }

        // Stale stat data would report the pre-write size of a file this same
        // request just replaced.
        clearstatcache(true, $path);
        $size = @filesize($path);

        return $size === false ? null : $size;
    }

    public function delete(string $key): bool
    {
        $path = $this->path($key);

        return is_file($path) && @unlink($path);
    }

    /**
     * Write to a sibling temp file, flush it to disk, then rename into place.
     *
     * rename(2) within one filesystem is atomic, so a concurrent reader sees either
     * no object or the whole object -- never a prefix. The fsync before the rename
     * is the other half: without it the rename can be visible while the bytes are
     * still only in the page cache, and a host that loses power leaves behind a
     * correctly-named empty file.
     *
     * The temp file is a sibling rather than a file in the system temp dir because
     * those are different mounts here. rename() across devices fails with EXDEV,
     * which is exactly the kind of error that only shows up in the container and
     * never on a developer's laptop.
     *
     * @param  callable(resource): void  $writer
     */
    private function write(string $key, callable $writer): void
    {
        $path = $this->path($key);
        $this->ensureDirectory(dirname($path));

        $temporaryPath = sprintf('%s.%s.part', $path, bin2hex(random_bytes(8)));
        $handle = @fopen($temporaryPath, 'xb');

        if ($handle === false) {
            throw new StorageException("Cannot create temporary file in {$this->root}.");
        }

        try {
            $writer($handle);

            if (! fflush($handle) || ! fsync($handle)) {
                throw new StorageException("Cannot flush {$key} to disk.");
            }

            fclose($handle);
            $handle = null;

            // Before the rename, so the object is never briefly visible under the
            // wrong mode. umask would otherwise decide this.
            @chmod($temporaryPath, self::FILE_MODE);

            if (! @rename($temporaryPath, $path)) {
                throw new StorageException("Cannot move temporary file into place for {$key}.");
            }
        } catch (Throwable $e) {
            if (is_resource($handle)) {
                fclose($handle);
            }

            // A crash between rename and the INSERT leaves an orphan blob, which a
            // sweep can reclaim. A partial file left under a real key cannot be told
            // apart from a valid one, so it must not survive this frame.
            @unlink($temporaryPath);

            throw $e instanceof StorageException
                ? $e
                : new StorageException("Cannot store {$key}: {$e->getMessage()}", previous: $e);
        }
    }

    private function ensureDirectory(string $directory): void
    {
        // The `audio` volume is empty on first boot, and FrankenPHP serves requests
        // on concurrent threads in one process -- so two uploads arriving together
        // both see a missing directory and both call mkdir, and the loser gets
        // false. The is_dir() re-check after a failed mkdir is the real test, not a
        // redundant one.
        if (is_dir($directory)) {
            return;
        }

        if (! @mkdir($directory, self::DIRECTORY_MODE, true) && ! is_dir($directory)) {
            throw new StorageException("Cannot create directory {$directory}.");
        }

        // mkdir()'s mode argument is masked by the process umask -- 02775 under the
        // default 022 lands as 0755 -- and Linux ignores the setgid bit there
        // outright, inheriting it from the parent instead. So the mode above is a
        // request, not a result, and this loop is what makes DIRECTORY_MODE true.
        //
        // Verified on a fresh volume before it existed: /data/audio arrived 2775
        // from the image, and every directory the first upload created under it came
        // out drwxr-sr-x. Group-readable, not group-writable -- so a worker sharing
        // only the `memo` group could read every blob and unlink none of them. That
        // is MEMO-12's EACCES again, one level below where the volume was fixed.
        //
        // Built downwards from the root, one key segment at a time, and that shape
        // is load-bearing rather than stylistic. The first version of this walked
        // *upwards* with dirname() and stopped once the path no longer started with
        // the root -- which with AUDIO_DIR=/ is never, because the constructor's
        // rtrim turns "/" into "" and every absolute path starts with "". That is an
        // infinite loop inside a request handler, chmod'ing / on its first pass.
        // Concatenating from the root instead cannot leave the root, cannot reach
        // the root itself, and is bounded by the number of segments in the key.
        //
        // The root is excluded on purpose: it is the volume mount point, and its
        // mode is seeded from the image at volume-creation time (api/Dockerfile) and
        // belongs to whoever created the volume, not to this write. chmod is
        // idempotent, which is also what makes this safe in the thread that just
        // lost the mkdir race above.
        if (! str_starts_with($directory, "{$this->root}/")) {
            return;
        }

        $relative = trim(substr($directory, strlen($this->root)), '/');

        if ($relative === '') {
            return;
        }

        $path = $this->root;

        foreach (explode('/', $relative) as $segment) {
            $path .= "/{$segment}";

            @chmod($path, self::DIRECTORY_MODE);
        }
    }

    /**
     * Keys are joined to the root here and nowhere else, so this is the only place
     * traversal has to be refused. A key reaches this method from an id we
     * generated, but "it is trusted today" is not a property that survives
     * refactoring.
     */
    private function path(string $key): string
    {
        if ($key === '' || str_contains($key, "\0")) {
            throw new StorageException('Storage key is empty or contains a null byte.');
        }

        if (str_starts_with($key, '/') || preg_match('#(^|/)\.\.(/|$)#', $key) === 1) {
            throw new StorageException("Storage key \"{$key}\" must be relative and may not traverse upwards.");
        }

        return "{$this->root}/{$key}";
    }
}

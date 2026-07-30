<?php

declare(strict_types=1);

namespace App\Contracts;

use App\Exceptions\StorageException;

/**
 * Audio blob storage, addressed by an opaque key.
 *
 * Why this exists rather than Storage::disk('audio'): Laravel's filesystem is
 * Flysystem, and Flysystem's write() is neither atomic nor fsynced. MEMO-11
 * requires the file to be complete on disk *before* the row referencing it is
 * inserted, because the Python worker claims rows and opens whatever path it
 * finds -- a half-written file is indistinguishable from a valid one. So the
 * durability guarantee lives here, in the contract, where an implementation has
 * to honour it.
 *
 * Two rules keep the S3 swap real:
 *
 *  1. Nothing outside an implementation may build a filesystem path. Callers hold
 *     keys; the key is what gets persisted in memos.audio_path, and the worker
 *     resolves it against its own AUDIO_DIR. A caller holding an absolute path has
 *     already bound itself to the local driver.
 *  2. Every write is atomic and durable before it returns.
 *
 * An S3 implementation would wrap Storage::disk('s3') -- which is the point where
 * Laravel's filesystem abstraction earns its place, since a single-part PUT is
 * already atomic and durable on the far side.
 */
interface AudioStorage
{
    /**
     * @throws StorageException
     */
    public function put(string $key, string $contents): void;

    /**
     * Store the contents of a file already on disk -- the PHP upload temp file.
     *
     * Separate from put() rather than a file_get_contents() away from it: the byte
     * cap is 12 MiB by default, and reading a blob into a string to write it
     * straight back out is memory this container does not need to spend.
     *
     * @throws StorageException
     */
    public function putFile(string $key, string $sourcePath): void;

    /**
     * @throws StorageException On a malformed key. Every method here validates the
     *                          key, including the read-only ones -- a driver must
     *                          never resolve a key it would refuse to write, or a
     *                          traversal probe could be used to test for files
     *                          outside the root even though it cannot create them.
     */
    public function exists(string $key): bool;

    /**
     * Null when the object does not exist.
     *
     * @throws StorageException On a malformed key.
     */
    public function size(string $key): ?int;

    /**
     * True when the object existed and is now gone.
     *
     * @throws StorageException On a malformed key.
     */
    public function delete(string $key): bool;
}

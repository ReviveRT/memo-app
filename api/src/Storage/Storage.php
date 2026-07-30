<?php

declare(strict_types=1);

namespace Memo\Storage;

/**
 * Audio blob storage, addressed by an opaque key.
 *
 * The seam exists so the local Docker volume can be swapped for object storage
 * without touching a controller or a service. Two rules keep that swap real:
 *
 *  1. Nothing outside this namespace may build a filesystem path. Callers hold
 *     keys; the key is what gets persisted in memos.audio_path, and the worker
 *     resolves it against its own AUDIO_DIR. A caller that has an absolute path
 *     has already bound itself to the local driver.
 *  2. Every write is atomic and durable before it returns. The row referencing a
 *     blob is inserted after the write, so a reader that sees a key must find a
 *     complete object behind it -- a worker claiming a half-written file is the
 *     failure this interface is shaped to make impossible.
 */
interface Storage
{
    /**
     * @throws StorageException
     */
    public function put(string $key, string $contents): void;

    /**
     * Store the contents of a file already on disk -- the PHP upload temp file.
     *
     * Separate from put() rather than a file_get_contents() away from it: the
     * byte cap is 12 MiB by default and reading a blob into a string to write it
     * straight back out is memory this container does not need to spend.
     *
     * @throws StorageException
     */
    public function putFile(string $key, string $sourcePath): void;

    public function exists(string $key): bool;

    /** Null when the object does not exist. */
    public function size(string $key): ?int;

    /** True when the object existed and is now gone. */
    public function delete(string $key): bool;
}

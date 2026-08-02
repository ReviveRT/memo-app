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

    /**
     * Where this object is on a local filesystem, or null when there is no such object.
     *
     * **The one method that admits a driver may be local, and it is here rather than at a
     * call site for exactly the reason rule 1 above gives.** MEMO-23 serves recordings back
     * with byte ranges, and the thing that does that -- Symfony's BinaryFileResponse, or a
     * web server handed an X-Accel-Redirect -- needs a path rather than a stream: it seeks
     * to an offset and copies a length, and a `readStream()` returning a handle would mean
     * writing that range arithmetic here instead of using the framework's. So the choice was
     * between an interface that hands out a path and a controller that builds one out of
     * config('memo.audio_dir'), and this is the half of that pair that keeps traversal
     * refused in one place and keeps the key the only thing callers hold.
     *
     * Null means *no such object*, and nothing else -- it is the missing-blob case the
     * playback route answers 404 for. A driver with no local filesystem must **throw**
     * rather than return null: an S3 implementation returning null here would turn every
     * playback request into "that recording is gone" instead of "this deployment cannot
     * serve audio this way", and the first is a lie that looks like data loss. What an S3
     * driver would do instead is answer with a presigned URL and let the controller redirect
     * to it -- S3 honours Range itself -- which is a second method and a second branch, and
     * is not worth building against a driver that does not exist yet.
     *
     * @throws StorageException On a malformed key, or from a driver that has no local
     *                          filesystem to answer with.
     */
    public function localPath(string $key): ?string;

    /**
     * A URL the browser may fetch this object from directly, or null from a driver that has
     * none.
     *
     * **The second method the localPath docblock above predicted, and it is the branch, not a
     * convenience.** Callers ask for this first and fall back to localPath, so a driver
     * answers exactly one of the two and never both: S3AudioStorage throws from localPath and
     * returns a URL here; LocalAudioStorage returns null here and a path there. A caller that
     * checked only one would work against one driver and 404 against the other.
     *
     * Null and an exception mean different things, the same way they do above. Null is "this
     * driver does not do that", which is a fact about the deployment and sends the caller to
     * localPath. An exception is a malformed key.
     *
     * **Whatever this returns is a bearer capability for the lifetime it is signed with.** It
     * carries no cookie and no owner check -- S3 has never heard of either -- so the
     * ownership decision has already been made by the time this is called, and anyone the URL
     * reaches can fetch those bytes until it expires. Keep $seconds short. It is the same
     * trade the claim link makes, on a much smaller scale and with a deadline.
     *
     * @param  int  $seconds  How long the URL stays valid.
     *
     * @throws StorageException On a malformed key.
     */
    public function temporaryUrl(string $key, int $seconds): ?string;
}

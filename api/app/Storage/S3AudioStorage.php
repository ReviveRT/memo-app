<?php

declare(strict_types=1);

namespace App\Storage;

use App\Contracts\AudioStorage;
use App\Exceptions\StorageException;
use Illuminate\Contracts\Filesystem\Filesystem;
use League\Flysystem\UnableToCheckExistence;
use Throwable;

/**
 * AudioStorage backed by an S3-compatible bucket. The swap point AppServiceProvider named and
 * AudioStorage's docblock predicted, built for one reason: **a free tier's filesystem is
 * ephemeral.**
 *
 * Every free platform rebuilds the container on deploy and most stop it when idle, and both
 * take the disk with them. With LocalAudioStorage that means a memo's transcript survives in
 * Postgres while its recording quietly disappears -- not on a schedule anyone announced, and
 * not visibly until somebody presses play on a memo from last week.
 *
 * **Why Flysystem here when LocalAudioStorage deliberately refuses it.** That class's opening
 * argument is that Flysystem's write() is neither atomic nor fsynced, and MEMO-11 needs the
 * blob complete before the row referencing it is inserted. That argument is about *local
 * files*, where a write is a sequence of syscalls a reader can catch halfway. It does not
 * transfer: a single-part S3 PUT is atomic on the far side by protocol -- the object does not
 * exist until the request completes, and no GET can observe a prefix of it -- and durability
 * is the service's problem once it has answered. So the same requirement is met by a
 * different mechanism, which is exactly the case the interface was drawn for.
 *
 * **Cloudflare R2 is the intended target and the reason `endpoint` exists.** S3 itself has no
 * free tier worth the name and bills for egress, which for audio is the whole cost; R2 has 10
 * GB of storage and zero egress fees. Both speak the same API, which is the entire reason
 * this class does not know which one it is talking to. deploy/README.md has the bucket setup.
 */
final class S3AudioStorage implements AudioStorage
{
    /**
     * @param  Filesystem  $disk  A configured S3 disk. Injected rather than resolved from
     *                            Storage::disk() inside each method, so this class depends on
     *                            the one thing it uses and a test can hand it a fake.
     *
     * Worth knowing before "fixing" the type: `temporaryUrl` is declared on **neither**
     * Filesystem nor Cloud -- it exists only on the concrete FilesystemAdapter, which is what
     * Storage::disk() actually returns. So a static analyser will flag the call below on any
     * interface type, and widening to Cloud does not help. It is exercised against a real
     * S3 implementation rather than trusted to the type system.
     */
    public function __construct(private readonly Filesystem $disk) {}

    public function put(string $key, string $contents): void
    {
        $this->guard($key);

        // Under Laravel's default `throw => false`, Flysystem answers a failed write with
        // `false` rather than raising -- which is the trap this checks for.
        // config/filesystems.php sets `throw => true` on the `audio` disk precisely so a
        // failed upload is an exception; this branch is the backstop for a disk configured
        // without it, so the two cannot disagree about whether a write happened.
        if ($this->disk->put($key, $contents) === false) {
            throw new StorageException("Could not write audio object {$key} to the bucket.");
        }
    }

    public function putFile(string $key, string $sourcePath): void
    {
        $this->guard($key);

        $handle = @fopen($sourcePath, 'rb');

        if ($handle === false) {
            throw new StorageException("Cannot open {$sourcePath} for reading.");
        }

        try {
            // A stream, not file_get_contents, for the reason the interface gives about
            // putFile existing at all: the cap is 12 MiB and reading a blob into a string to
            // hand it straight to a socket is memory this container does not need to spend.
            // It matters more here than locally -- the AWS SDK will chunk a stream up to the
            // wire rather than holding a second copy.
            if ($this->disk->writeStream($key, $handle) === false) {
                throw new StorageException("Could not write audio object {$key} to the bucket.");
            }
        } finally {
            // Flysystem closes the handle on the success path; closing an already-closed
            // resource is a warning rather than an error, so this is guarded rather than
            // unconditional. The finally is what covers the throw above.
            if (is_resource($handle)) {
                fclose($handle);
            }
        }
    }

    public function exists(string $key): bool
    {
        $this->guard($key);

        try {
            return $this->disk->exists($key);
        } catch (UnableToCheckExistence $e) {
            // **Not folded into false, and this is the important one.** A bucket that is
            // unreachable, misconfigured or refusing credentials answers the same "no" as an
            // object that was never uploaded, and the caller acts on that difference: false
            // reads as "this memo has no recording", which is a 404 telling somebody their
            // audio is gone. A broken deployment must not be able to say that.
            throw new StorageException("Could not reach the audio bucket to look up {$key}.", 0, $e);
        }
    }

    public function size(string $key): ?int
    {
        $this->guard($key);

        try {
            return $this->disk->exists($key) ? $this->disk->size($key) : null;
        } catch (UnableToCheckExistence $e) {
            throw new StorageException("Could not reach the audio bucket to size {$key}.", 0, $e);
        }
    }

    public function delete(string $key): bool
    {
        $this->guard($key);

        // exists() first, because the contract is "existed and is now gone" and S3's DELETE
        // is idempotent -- it answers 204 for a key that was never there, so the delete's own
        // return value cannot distinguish the two. Two round trips, on an operation that
        // happens once per deleted memo.
        if (! $this->exists($key)) {
            return false;
        }

        return $this->disk->delete($key);
    }

    /**
     * Always throws. There is no local filesystem behind this driver.
     *
     * **Throwing rather than returning null is required by the interface, and the reason is
     * worth repeating where somebody might "fix" it.** Null means *no such object*, which the
     * playback route renders as "the recording for that memo is no longer on the audio
     * volume" -- so a null here would tell every user of an S3 deployment that all of their
     * recordings had been lost, when in fact the caller simply asked the wrong question. The
     * right question is temporaryUrl, which MemoController::audio asks first.
     */
    public function localPath(string $key): ?string
    {
        $this->guard($key);

        throw new StorageException(
            'This deployment stores audio in a bucket, which has no local path. '
            .'MemoController::audio must redirect to temporaryUrl() instead.'
        );
    }

    public function temporaryUrl(string $key, int $seconds): ?string
    {
        $this->guard($key);

        try {
            return $this->disk->temporaryUrl($key, now()->addSeconds($seconds));
        } catch (Throwable $e) {
            // Signing is local arithmetic and does not touch the network, so the realistic
            // cause is a disk configured without credentials -- which Laravel reports as a
            // RuntimeException about the driver not supporting temporary URLs. That is a
            // deployment fault and a 500, not a missing recording.
            throw new StorageException("Could not sign a playback URL for {$key}.", 0, $e);
        }
    }

    /**
     * Refuse a key this driver should never resolve.
     *
     * Weaker than LocalAudioStorage::path by necessity and stronger than S3 requires. There is
     * no traversal to prevent -- an S3 key is an opaque string and `../` in it is two literal
     * dots, not a parent directory -- so nothing here is protecting the bucket. It is here so
     * that the two drivers refuse the same inputs: a key that works against a bucket and
     * throws against a volume would mean a deployment could accumulate objects that stop being
     * addressable the moment it moves back to local storage, and the failure would appear long
     * after the change that caused it.
     *
     * The leading-slash check is not only for symmetry: S3 permits a key beginning with `/`,
     * and it produces a bucket with an empty-named top-level folder that most tooling cannot
     * display.
     */
    private function guard(string $key): void
    {
        if ($key === '' || str_contains($key, "\0")) {
            throw new StorageException('Storage key is empty or contains a null byte.');
        }

        if (str_starts_with($key, '/') || preg_match('#(^|/)\.\.(/|$)#', $key) === 1) {
            throw new StorageException("Storage key \"{$key}\" must be relative and may not traverse upwards.");
        }
    }
}

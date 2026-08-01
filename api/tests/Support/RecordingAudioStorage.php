<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Contracts\AudioStorage;
use App\Exceptions\StorageException;

/**
 * An AudioStorage that keeps blobs in an array and remembers the order it was asked.
 *
 * The real driver is not faked away here for the sake of speed -- LocalAudioStorage is
 * fast, and SharedAudioVolumeTest already drives it against a temp directory. It is
 * faked so that a test can assert the two things about a *voice memo* that a
 * filesystem cannot show: that the blob was written before the row referencing it, and
 * that the key the row carries is the key the bytes went to. Both are properties of
 * MemoService's ordering rather than of any driver, and the second one is the whole
 * contract between the API and the Python worker.
 *
 * $failOnPut is the other half. The orphan-blob decision in MemoService::createFromAudio
 * only has a case to answer when a write succeeds and the INSERT does not, and a
 * storage that can be made to fail on demand is what lets the opposite case -- an
 * unwritable volume -- be checked for reaching the caller rather than being swallowed.
 */
final class RecordingAudioStorage implements AudioStorage
{
    /** @var array<string, string> Key to contents. */
    public array $blobs = [];

    /** Every key written, in order. @var list<string> */
    public array $written = [];

    /** Set to make the next put/putFile throw, standing in for a full or unmounted volume. */
    public bool $failOnPut = false;

    public function put(string $key, string $contents): void
    {
        if ($this->failOnPut) {
            throw new StorageException("Refusing to store {$key}: this is a test double.");
        }

        $this->blobs[$key] = $contents;
        $this->written[] = $key;
    }

    public function putFile(string $key, string $sourcePath): void
    {
        // Read eagerly rather than storing the path. The path is PHP's upload temp
        // file, which is unlinked when the request that created it ends -- so a test
        // asserting on the contents afterwards would be reading a file that is gone.
        // Reading here is also what pins that the source is readable at the moment
        // MemoService hands it over, which is the ordering the real driver depends on.
        $contents = @file_get_contents($sourcePath);

        if ($contents === false) {
            throw new StorageException("Cannot read source file {$sourcePath}.");
        }

        $this->put($key, $contents);
    }

    public function exists(string $key): bool
    {
        return array_key_exists($key, $this->blobs);
    }

    public function size(string $key): ?int
    {
        return $this->exists($key) ? strlen($this->blobs[$key]) : null;
    }

    public function delete(string $key): bool
    {
        if (! $this->exists($key)) {
            return false;
        }

        unset($this->blobs[$key]);

        return true;
    }
}

<?php

declare(strict_types=1);

namespace App\Services\Memos;

/**
 * The recording behind a memo: where the bytes are kept, and what they are.
 *
 * **A second shape for two columns that are already on the table, and the argument is
 * DeletedMemo's.** `audio_path` is a storage key -- an implementation detail of whichever
 * AudioStorage is configured -- so it is deliberately not on Memo and therefore not in any
 * memo response; putting it there would be an invitation to build a URL out of it, which
 * App\Contracts\AudioStorage forbids in as many words. But the playback route does need
 * both values, and it needs them without the projection every other route answers with:
 * the transcript of a five-minute memo is a lot of text to read in order to serve a file.
 *
 * So this is the *storage* view of a memo, the way Memo is the *response* view. One
 * statement, two columns, and neither of them ever reaches a client.
 *
 * `mimeType` is nullable because the column is. In practice the two are written together
 * by one caller (MemoService::createFromAudio) and MemoRepository::insert says why they
 * are not independently optional -- but "in practice" is a property of today's writers,
 * and a row hand-inserted with a path and no mime would otherwise be a TypeError inside a
 * request rather than a recording served as the octet-stream it is.
 */
final class MemoAudio
{
    /**
     * @param  string  $key  Relative to AUDIO_DIR, never an absolute path. Resolved by
     *                       AudioStorage::localPath() and by nothing else.
     * @param  ?string  $mimeType  Sniffed from the bytes at upload time by
     *                             App\Http\Rules\SniffedAudioType, which is the same rule
     *                             that vouched for the file being a recording at all --
     *                             so the value served back is one this app allowed in
     *                             rather than one a client named.
     */
    public function __construct(
        public readonly string $key,
        public readonly ?string $mimeType,
    ) {}
}

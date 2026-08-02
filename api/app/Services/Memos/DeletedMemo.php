<?php

declare(strict_types=1);

namespace App\Services\Memos;

/**
 * A memo that has just been removed, and the audio key that went with it.
 *
 * **Two fields rather than one, because `audio_path` may not be part of a Memo.** It is a
 * storage key -- an implementation detail of whichever AudioStorage is configured -- and
 * putting it on Memo would put it in every API response, where it is at best noise and at
 * worst an invitation to build a URL out of it. AudioStorage's contract is explicit that
 * nothing outside an implementation may do that.
 *
 * But the one caller that deletes a memo does need it: the row is gone, and the row is the
 * only thing that knew which blob belonged to it. Reading the path in a SELECT before the
 * DELETE would be a race -- two clients deleting the same memo would both read it and both
 * try to unlink -- so the path comes back from the DELETE itself, in the same statement, and
 * this is what carries it the one hop from the repository to the service.
 *
 * Null `audioPath` for a typed memo, which never had a blob. That is the ordinary case rather
 * than an error, and it is why MemoService checks instead of assuming.
 */
final class DeletedMemo
{
    public function __construct(
        public readonly Memo $memo,
        public readonly ?string $audioPath,
    ) {}
}

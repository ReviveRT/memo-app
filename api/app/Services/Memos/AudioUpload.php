<?php

declare(strict_types=1);

namespace App\Services\Memos;

/**
 * One recording, as it reaches the service layer: bytes on disk, what they turned
 * out to be, and what to call the file.
 *
 * This exists so that MemoService never sees an UploadedFile. The rest of this
 * namespace decides what a memo is and knows nothing about HTTP -- MemoController
 * says as much about itself -- and a Symfony upload object carries the request with
 * it: the client's filename, the client's Content-Type, the PHP error code. Handing
 * one to the service would put "which of those do we believe?" inside the layer that
 * is supposed to have already been told.
 *
 * So the answering happens at the edge, in StoreMemoRequest::audio(), and what
 * arrives here is the answer. That is also the seam MEMO-11 builds on: it adds the
 * byte cap and the sniffed-MIME allowlist, and both are decisions about whether one
 * of these may be constructed at all, not about what MemoService then does with it.
 */
final class AudioUpload
{
    /**
     * @param  string  $path  The PHP upload temp file. Read once, by AudioStorage,
     *                        and never persisted -- PHP unlinks it when the request
     *                        ends, so nothing may hold this past the response.
     * @param  string  $mimeType  Sniffed from the bytes, not the client's label. This
     *                            is what lands in `memos.audio_mime`, and MEMO-23
     *                            serves the blob back under it.
     * @param  string  $extension  Already narrowed to a safe token by
     *                             StoreMemoRequest::audio(); it becomes part of a
     *                             storage key and therefore part of a filesystem path.
     */
    public function __construct(
        public readonly string $path,
        public readonly string $mimeType,
        public readonly string $extension,
    ) {}
}

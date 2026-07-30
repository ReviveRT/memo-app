<?php

declare(strict_types=1);

namespace Memo\Service;

/**
 * The effective PHP upload limits next to the cap the application enforces.
 *
 * Reported by /api/health because this is the mismatch with no symptom: when
 * MAX_AUDIO_BYTES exceeds post_max_size, PHP discards the request body and hands
 * the handler an empty $_FILES *and* an empty $_POST with no error flag set
 * anywhere (MEMO-11). Nothing logs, nothing 500s, and the only visible effect is
 * that recordings above some unstated size vanish. Three numbers on the
 * healthcheck turn that into something you can see before it happens.
 */
final class UploadLimits
{
    private function __construct(
        public readonly int $maxAudioBytes,
        public readonly int $uploadMaxFilesize,
        public readonly int $postMaxSize,
    ) {
    }

    public static function current(int $maxAudioBytes): self
    {
        return new self(
            $maxAudioBytes,
            // ini_parse_quantity handles the shorthand notation ("16M"), which
            // ini_get returns verbatim.
            ini_parse_quantity((string) ini_get('upload_max_filesize')),
            ini_parse_quantity((string) ini_get('post_max_size')),
        );
    }

    /** Whether a memo at exactly MAX_AUDIO_BYTES can physically reach a handler. */
    public function acceptsMaxAudio(): bool
    {
        if ($this->uploadMaxFilesize < $this->maxAudioBytes) {
            return false;
        }

        // 0 means unlimited for post_max_size only. Strictly greater than, not at
        // least: the multipart envelope around the file counts against this one.
        return $this->postMaxSize === 0 || $this->postMaxSize > $this->maxAudioBytes;
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return [
            'max_audio_bytes' => $this->maxAudioBytes,
            'upload_max_filesize' => $this->uploadMaxFilesize,
            'post_max_size' => $this->postMaxSize,
            'accepts_max_audio' => $this->acceptsMaxAudio(),
        ];
    }
}

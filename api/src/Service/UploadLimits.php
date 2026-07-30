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
        return new self($maxAudioBytes, self::bytes('upload_max_filesize'), self::bytes('post_max_size'));
    }

    /**
     * ini_parse_quantity handles the shorthand notation ("16M") that ini_get
     * returns verbatim.
     *
     * The false branch matters more than it looks. ini_get returns false for a
     * directive it cannot read, and casting that to a string parses as 0 -- which
     * means "unlimited" for post_max_size. An unreadable directive would then be
     * reported as no limit at all and accepts_max_audio would come back true: a
     * false all-clear on the one field whose entire job is to catch a silent
     * misconfiguration. -1 fails every comparison below instead.
     */
    private static function bytes(string $directive): int
    {
        $value = ini_get($directive);

        return $value === false ? -1 : ini_parse_quantity($value);
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

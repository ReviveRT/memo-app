<?php

declare(strict_types=1);

namespace App\Http\Rules;

use Closure;
use Illuminate\Contracts\Validation\ValidationRule;
use Illuminate\Http\UploadedFile;

/**
 * Refuses an upload whose *bytes* are not a container this app can transcribe.
 *
 * The whole rule is "match on the sniffed type, never on the client label", and the
 * label is not merely untrustworthy in the adversarial sense: Firefox reports
 * `audio/webm` from MediaRecorder and produces Ogg (Mozilla bug 1501308), so the
 * honest client is already wrong about a third of real recordings. What arrives is
 * decided by reading the file.
 *
 * **Why the list looks nothing like `audio/*`.** Every mapping in this table was read
 * off this image's libmagic rather than assumed -- the allowlist itself has four more
 * entries and ALLOWED says which of them were not -- and the surprise is that two of the
 * three browser containers sniff as `video/`:
 *
 *   Chrome, Edge   EBML with DocType "webm"   -> video/webm
 *   Firefox        Ogg carrying Opus          -> audio/ogg
 *   Safari         MP4 with an `ftyp` box     -> video/mp4
 *   m4a            MP4 with an `M4A ` brand   -> audio/x-m4a
 *   mp3            an MPEG audio frame        -> audio/mpeg
 *   wav            RIFF/WAVE                  -> audio/x-wav
 *
 * An audio-only WebM and an audio-only MP4 are the same container as the video ones
 * with no video track in them, and libmagic reads the container. So the obvious
 * allowlist -- `audio/webm`, `audio/wav` -- rejects every genuine recording from
 * Chrome, Edge and Safari, which is to say the feature.
 *
 * The consequence to state plainly rather than leave implied: an actual video file is
 * accepted, because nothing here can tell it from the audio-only recording it is
 * byte-identical to at the header. That is fine and not a hole -- MEMO-13's ffmpeg pass
 * takes the audio stream and discards the rest, and the byte cap in StoreMemoRequest
 * bounds what can be sent either way.
 *
 * The other half of the upload edge is the byte cap, in
 * App\Http\Requests\StoreMemoRequest -- named in prose rather than as a `@see` tag
 * because that class imports this one, and an import back would be a cycle bought for a
 * docblock.
 */
final class SniffedAudioType implements ValidationRule
{
    /**
     * The containers this app accepts, by sniffed type.
     *
     * The first seven are MEMO-11's list and each was reproduced against this image's
     * libmagic. `application/ogg` is the one that was not: neither a Theora Ogg
     * (video/ogg), a FLAC Ogg (audio/ogg) nor an Ogg with an unrecognised first packet
     * (application/octet-stream) produced it here. It is kept because the task verified
     * it somewhere and because a multiplexed Ogg is exactly the shape a different magic
     * database would answer it for -- an entry that never matches costs nothing, and a
     * missing one rejects a real recording.
     *
     * The last three are deliberate alternates for containers already on the list, not
     * new formats. libmagic's answer for a given container is a property of the magic
     * database rather than of the file, and it moves between releases: `audio/x-wav` and
     * `audio/wav` are the same RIFF, `video/mp4` and `audio/mp4` the same `ftyp` box.
     * Being one release behind on that vocabulary would reject every recording from a
     * browser rather than degrade, which is worth three entries that assert nothing new
     * about what may be uploaded.
     */
    public const ALLOWED = [
        // Verified on this image.
        'video/webm',
        'audio/ogg',
        'application/ogg',
        'video/mp4',
        'audio/x-m4a',
        'audio/mpeg',
        'audio/x-wav',

        // Alternates for those same containers, in case libmagic names them differently.
        'audio/webm',
        'audio/mp4',
        'audio/wav',
    ];

    /**
     * What an upload's bytes actually are: sniffed, lowercased, parameters stripped, or
     * null when there is no usable answer.
     *
     * This is the single definition of an upload's type, used both by the check below
     * and by StoreMemoRequest::audio() for the value that lands in `memos.audio_mime` --
     * so the type that was allowed is the type that gets stored, and MEMO-23 serves the
     * blob back under a value this rule vouched for.
     *
     * **The `;codecs=` strip.** MEMO-11 calls for it and it is defensive here rather than
     * load-bearing: a codecs parameter is something a *client* writes
     * (`audio/webm;codecs=opus` is what MediaRecorder reports), and `getMimeType()` is
     * finfo under FILEINFO_MIME_TYPE, which answers a bare type. The sibling constant
     * FILEINFO_MIME does append parameters, one flag away, and the cost of not depending
     * on which one Symfony passes is this line.
     *
     * The token check is the same reasoning applied to the other end: this string is
     * echoed back to the caller in the failure message and stored in a text column, and
     * it comes from a magic database rather than from anywhere in this repo. Anything
     * that is not shaped like a media type is treated as no answer at all, which the
     * allowlist would have refused anyway.
     */
    public static function of(UploadedFile $file): ?string
    {
        // Only for an upload PHP completed. getMimeType() runs finfo over getPathname(),
        // which is the empty string when the upload failed -- so asking a failed upload
        // what it contains gets a warning and a null rather than an answer. Those are
        // routed by StoreMemoRequest before validation ever runs; this is here so the
        // method is honest on its own.
        if (! $file->isValid()) {
            return null;
        }

        $mimeType = $file->getMimeType();

        if (! is_string($mimeType)) {
            return null;
        }

        $mimeType = strtolower(trim(explode(';', $mimeType, 2)[0]));

        // RFC 6838's restricted-name character set, which is narrower than it looks:
        // every type in ALLOWED matches, and nothing with a space, a quote or a control
        // character in it does.
        //
        // `~` as the delimiter, not the `#` used elsewhere in this repo: `#` is itself a
        // legal character in a restricted name, so it appears inside the class below and
        // ends the pattern early. That is not a subtle failure -- preg_match() raises
        // "Unknown modifier '$'" and returns false, which reads as "this file is not a
        // recording" and rejects every upload.
        return preg_match('~^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$~', $mimeType) === 1
            ? $mimeType
            : null;
    }

    public function validate(string $attribute, mixed $value, Closure $fail): void
    {
        // Not a file at all, or an upload that failed: `file` in the same rule list says
        // that better than this rule can, and both messages would otherwise be shown.
        if (! $value instanceof UploadedFile || ! $value->isValid()) {
            return;
        }

        $mimeType = self::of($value);

        if ($mimeType !== null && in_array($mimeType, self::ALLOWED, true)) {
            return;
        }

        // The frontend renders this verbatim under the Record button
        // (web/src/api/memos.js), so it has to read as a sentence about the file rather
        // than as a list of media types the reader never chose. The sniffed type is
        // named because it is the one piece of information they do not have -- "not a
        // recording" on its own invites a second attempt with the same file.
        //
        // No :attribute placeholder. "The audio field" is a name for a multipart part,
        // and nobody who pressed Record knows they sent one; ListMemosRequest renames
        // its own field for the same reason.
        //
        // No dashes either: useMemos prefixes this with "Could not upload the recording
        // — " before it reaches the screen, and a sentence carrying its own em-dashes on
        // top of that stops being one anybody parses.
        $accepted = ' Recordings from Chrome, Edge, Safari and Firefox are all accepted as they are.';

        $fail($mimeType === null
            ? 'That file is not a recording this app can read.'.$accepted
            : "That file is not a recording this app can read: it is {$mimeType}.".$accepted);
    }
}

<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\NoNullBytes;
use App\Services\Memos\AudioUpload;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Http\UploadedFile;

/**
 * Validation for POST /api/memos, which accepts either a typed memo or a recording.
 *
 * No authorize() method, and that is not an omission: this app has no
 * authentication by design (README, "Assumptions"), and FormRequest treats an
 * absent authorize() as granted rather than denied.
 *
 * One route with two accepted bodies rather than a /api/memos/audio of its own, for
 * the reason routes/api.php gives: both produce one memo row, in one table, with one
 * response shape, and differ only in which of `transcript` and `audio_path` starts
 * out set. Two routes would be two response shapes for the frontend to reconcile.
 *
 * **What this class does not do yet, on purpose.** MEMO-11 owns the upload edge and
 * every part of it is missing here: there is no byte cap (config('memo.max_audio_bytes')
 * is unread by this route today), no MIME allowlist, and no handling for the case
 * where PHP has discarded the body before Laravel booted -- an empty $_FILES against
 * a large CONTENT_LENGTH, which must answer 413 rather than a 422 about a missing
 * field. What stands in for all of it meanwhile is api/conf.d/uploads.ini: PHP
 * refuses anything over `upload_max_filesize` itself, and `file` below rejects the
 * failed upload it hands back. So the effective cap today is 16 MiB at the PHP layer
 * rather than the 12 MiB the application means, and the difference is exactly
 * MEMO-11's to close.
 */
final class StoreMemoRequest extends FormRequest
{
    /**
     * The upload field, named once so the rules, the accessors and the frontend's
     * FormData key cannot drift apart.
     */
    public const AUDIO_FIELD = 'audio';

    /**
     * The last-resort extension for a recording whose container we cannot name.
     *
     * Reachable in principle -- Symfony guesses the extension from the sniffed MIME
     * and can come back with nothing -- and not reachable from any of the three
     * browsers this task targets, all of which produce a container finfo knows. It
     * exists so that an unguessable upload gets a boring storage key instead of no
     * key at all: the bytes are still transcribable, since ffmpeg (MEMO-13) and every
     * STT provider sniff the container rather than reading the name.
     */
    private const FALLBACK_EXTENSION = 'bin';

    /**
     * A typed memo, not a document.
     *
     * A hardcoded constant rather than another environment variable: nothing in the
     * README's table configures it, the number needs no per-deployment tuning, and
     * MAX_AUDIO_BYTES already demonstrates what an env-configured cap costs in
     * places that have to agree with it.
     *
     * The number is not arbitrary. `transcript` is unbounded `text` in Postgres, so
     * the column will take anything -- but the transcript is also what MEMO-21 sends
     * to Claude for a title, a summary and tags. An uncapped field is therefore an
     * uncapped prompt on a paid API, reachable by an unauthenticated POST. 10,000
     * characters is on the order of 2,500 tokens of English -- more in a script that
     * tokenises less efficiently, since the cap counts characters rather than bytes
     * or tokens -- which is far more than anyone types into a memo box and still
     * bounded enough that a scripted flood is a nuisance rather than a bill.
     */
    public const MAX_TEXT_LENGTH = 10_000;

    /**
     * Trimmed before validation, not after.
     *
     * Laravel's global TrimStrings and ConvertEmptyStringsToNull middleware already
     * turn a whitespace-only `text` into null, so `required` would reject it anyway.
     * This does it again here on purpose: the rule that a memo cannot be blank is
     * this class's, and leaving it to middleware makes it silently revocable from
     * bootstrap/app.php by someone with an unrelated reason to change the global
     * stack. Doing it in prepareForValidation rather than in an accessor also means
     * `min:1` judges the same string that gets stored, instead of passing on padding
     * that trim() would remove afterwards.
     */
    protected function prepareForValidation(): void
    {
        $text = $this->input('text');

        if (is_string($text)) {
            $this->merge(['text' => trim($text)]);
        }
    }

    /**
     * @return array<string, list<mixed>>
     */
    public function rules(): array
    {
        return [
            // min:1 after the trim above is what rejects "   ". Laravel counts
            // characters with mb_strlen for a string under a `string` rule, so
            // max: is multibyte-safe and an emoji costs one character rather than
            // four.
            //
            // required_without rather than required, so a recording may arrive with no
            // `text` part at all. The other rules are not implicit, so Laravel skips
            // them for an attribute that is absent from the request -- which is what
            // makes this safe to relax without also marking the field nullable. A
            // `text` part that is *present* and empty is a different thing and still
            // fails, because ConvertEmptyStringsToNull turns it into a null that is
            // there, and `string` refuses it.
            'text' => [
                'required_without:'.self::AUDIO_FIELD,

                // Paired with the rule above, not a relaxation of it. `nullable` only
                // affects rules that are not implicit, and required_without is implicit
                // -- so a null `text` with no audio still fails, and a null `text`
                // alongside audio now stops running the string rules against nothing.
                //
                // The case is a multipart body carrying an empty `text` part as well as
                // the file. Laravel's ConvertEmptyStringsToNull turns that into a null
                // that is *present*, which is different from absent: `string` then ran
                // and answered "The text field must be a string." to a request whose
                // only content was a recording. Nothing this app sends looks like that
                // -- api/memos.js appends the file and nothing else -- but it is the
                // obvious shape for anyone building the same request by hand, and being
                // told the wrong field is wrong is a bad first answer to give them.
                'nullable',

                'string',
                'min:1',
                'max:'.self::MAX_TEXT_LENGTH,

                // A NUL here is silently destructive rather than fatal: libpq truncates
                // the bound parameter at it, so this endpoint answered 201 with a
                // one-character transcript for a three-character POST. The rule carries
                // the rest of that story, and ListMemosRequest shares it because the
                // truncation belongs to the driver rather than to either field.
                new NoNullBytes,
            ],

            self::AUDIO_FIELD => [
                'required_without:text',

                // Exclusive, not merged. A request carrying both would have to decide
                // whether the typed text is the transcript of the recording or a second
                // memo, and there is no answer to that which is not an invention -- so
                // it is refused loudly instead of one half being dropped in silence.
                // Nothing sends both today; this is here so that a client which starts
                // to finds out on the first request rather than by noticing later that
                // its audio never arrived.
                'prohibits:text',

                // The whole of the file validation for now. It rejects an upload PHP
                // marked failed -- which is how a file over upload_max_filesize
                // arrives, populated with UPLOAD_ERR_INI_SIZE rather than absent -- and
                // nothing else. The cap and the allowlist are MEMO-11's; see the class
                // note above for what that leaves open.
                'file',
            ],
        ];
    }

    /**
     * The validated text, or null when this request carried a recording instead.
     *
     * Null rather than an empty string, so the controller's branch is on the same
     * absence the rules are written against and cannot be fooled by a falsy '0'.
     */
    public function text(): ?string
    {
        $text = $this->validated()['text'] ?? null;

        return is_string($text) ? $text : null;
    }

    /**
     * The recording, resolved from the request to what the service layer needs, or
     * null when this request carried text instead.
     *
     * Two of the three fields are decided here rather than being taken as given, and
     * both choices are the ones MEMO-11 goes on to enforce:
     *
     *   * **The MIME is sniffed, not read off the request.** getMimeType() runs finfo
     *     over the bytes; getClientMimeType() is a header the client wrote, and a
     *     recording's Content-Type is unreliable even when the client is honest --
     *     Firefox produces Ogg while MediaRecorder reports WebM (Mozilla bug 1501308),
     *     so believing the label would store a mime that contradicts the file for every
     *     Firefox memo. This value lands in `memos.audio_mime` and MEMO-23 serves the
     *     blob back under it.
     *
     *   * **The extension is derived from that, then narrowed.** extension() maps the
     *     sniffed type through Symfony's own table, so it is a value from a closed
     *     vocabulary rather than anything the client chose -- clientOriginalExtension()
     *     is the untrusted one, and this string becomes part of a filesystem path. The
     *     regex is belt and braces over that: LocalAudioStorage refuses a key that
     *     traverses upwards, but "the vocabulary is closed today" is not a property that
     *     survives a Symfony upgrade, and the cost of not relying on it is one match.
     *
     * getMimeType() can return null for bytes finfo cannot place; the fallback is the
     * generic binary type rather than the client's label, on the same reasoning.
     */
    public function audio(): ?AudioUpload
    {
        $file = $this->file(self::AUDIO_FIELD);

        if (! $file instanceof UploadedFile) {
            return null;
        }

        $extension = (string) $file->extension();

        return new AudioUpload(
            // getPathname(), not getRealPath(): the latter is documented to return
            // false when the path cannot be resolved, which would be a TypeError
            // against AudioUpload's `string` rather than a readable failure. There is
            // no symlink to resolve on an upload temp file, so the two are the same
            // string on every path that reaches here.
            path: $file->getPathname(),
            mimeType: $file->getMimeType() ?? 'application/octet-stream',
            extension: preg_match('/^[a-z0-9]{1,8}$/', $extension) === 1
                ? $extension
                : self::FALLBACK_EXTENSION,
        );
    }
}

<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Exceptions\StorageException;
use App\Http\Rules\NoNullBytes;
use App\Http\Rules\SniffedAudioType;
use App\Http\Rules\SupportedLanguage;
use App\Services\Memos\AudioUpload;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Http\UploadedFile;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpKernel\Exception\HttpException;

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
 * **The upload edge (MEMO-11).** What the rules below cannot express is refused before
 * validation runs, in rejectUnacceptableUpload():
 *
 *   * **Size, as 413 rather than 422.** `max:` would say "the audio field must not be
 *     greater than 12288 kilobytes", which is a 422 about a form field; a body that
 *     exceeded what the server will take is a 413 about the request. Handled in
 *     prepareForValidation because the answer does not depend on any other field --
 *     and because the interesting cases arrive with no usable field to attach an error
 *     to at all.
 *   * **A body PHP threw away**, which is a 413 with nothing to measure. See
 *     rejectDiscardedBody().
 *   * **An upload PHP could not store**, which is a 500 rather than either of those:
 *     a full disk is not something the person recording can fix.
 *
 * Both size branches exist because the same misleading sentence came out of two
 * different failures. Verified against a live container before any of this: a 17 MB
 * upload -- over `upload_max_filesize`, so PHP keeps the part and drops the bytes --
 * answered 422 with "The text field is required when audio is not present." as its
 * message, and so did a 13 MB body PHP could not parse into a file at all. Advice to
 * type something, given twice, to somebody who had just uploaded megabytes.
 *
 * What the rules do still own is the MIME allowlist, which is a 422: the request was
 * read, and its contents were not acceptable. App\Http\Rules\SniffedAudioType.
 *
 * api/conf.d/uploads.ini is the other half of this and is not optional. PHP's own
 * limits sit above the application's on purpose -- 16M and 20M against a 12 MiB cap --
 * so that an over-cap recording reaches this class to be answered properly instead of
 * being dropped by PHP with nothing left to branch on.
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
        // Before the trim, and before any rule runs. Everything below this line assumes
        // the request survived PHP intact and is small enough to be worth reading.
        $this->rejectUnacceptableUpload();

        $text = $this->input('text');

        if (is_string($text)) {
            $this->merge(['text' => trim($text)]);
        }
    }

    /**
     * The refusals that never get to be validation errors, because by the time the
     * rules run there is nothing left for them to judge.
     *
     * Ordered by how early the failure happened, which is also the order in which the
     * evidence disappears: a discarded body leaves nothing but a header, a failed upload
     * leaves an error code and no bytes, and an oversized one leaves a real file to
     * measure. Reading them in the other order means asking a file that is not there how
     * big it is.
     */
    private function rejectUnacceptableUpload(): void
    {
        $file = $this->file(self::AUDIO_FIELD);

        if (! $file instanceof UploadedFile) {
            $this->rejectDiscardedBody();

            return;
        }

        $error = $file->getError();

        // UPLOAD_ERR_INI_SIZE is the file being over `upload_max_filesize`, and
        // UPLOAD_ERR_FORM_SIZE the same thing against a MAX_FILE_SIZE field in the form.
        // The first arrives with size 0 and an empty tmp_name -- PHP kept the part and
        // threw the bytes away -- so there is nothing to measure and the message cannot
        // name a size. Verified against this image at 17 MB, which is over
        // upload_max_filesize and under post_max_size.
        //
        // FORM_SIZE is grouped with it rather than separately verified: nothing this app
        // sends includes a MAX_FILE_SIZE field, so it is only reachable from a
        // hand-written form, and "the client declared a smaller limit and the file
        // exceeded it" is the same answer either way. Reading the size instead of
        // trusting the code would not help -- the point of both codes is that there is no
        // file left.
        if ($error === UPLOAD_ERR_INI_SIZE || $error === UPLOAD_ERR_FORM_SIZE) {
            throw $this->tooLarge(null);
        }

        // Not the caller's fault and not fixable by re-recording: PHP could not write
        // the upload to its temp directory, or an extension refused it. A 4xx here would
        // tell somebody their memo was the problem when the container's disk is full.
        // The same line MemoController draws for an unwritable audio volume, drawn one
        // step earlier -- see App\Exceptions\StorageException, which exists for it.
        if (in_array($error, [UPLOAD_ERR_NO_TMP_DIR, UPLOAD_ERR_CANT_WRITE, UPLOAD_ERR_EXTENSION], true)) {
            throw new StorageException("PHP could not receive the upload (UPLOAD_ERR {$error}).");
        }

        // Whatever is left -- UPLOAD_ERR_OK, or UPLOAD_ERR_PARTIAL from a connection
        // that dropped mid-body -- has a real file behind it, so the size is knowable.
        // getSize() is asked only now: it stats getPathname(), which is the empty string
        // for every error above.
        $size = $file->getSize();

        if (is_int($size) && $size > $this->maxAudioBytes()) {
            throw $this->tooLarge($size);
        }
    }

    /**
     * A body that declared more bytes than a memo may contain, and out of which PHP
     * handed us no file at all.
     *
     * The framework already answers the main case: ValidatePostSize is in the global
     * stack and throws 413 when CONTENT_LENGTH exceeds `post_max_size`, which is the
     * exact condition PHP discards a body on. This is the backstop for a request that
     * gets past it and still arrives empty, and the gap it covers is real rather than
     * theoretical -- everything between MAX_AUDIO_BYTES and post_max_size that PHP could
     * not turn into an upload. Verified live at 13 MB: a multipart body with no boundary
     * in its Content-Type, and a body sent with no Content-Type at all, both reach here
     * with $_FILES empty.
     *
     * The threshold is the application's own cap rather than post_max_size, because at
     * that size the answer is 413 whatever the cause: a request larger than any memo may
     * be, carrying no file, has nothing left that this route could accept.
     *
     * **Only the size case.** A *small* body that PHP could not parse -- the same
     * missing multipart boundary at 30 KB -- is not covered and is deliberately left as
     * the 422 it already is. It cannot be told apart here from a request that genuinely
     * attached nothing, and "the audio field is required" is a fair description of both.
     * web/src/api/memos.js carries the same warning from the client side, which is where
     * the mistake is actually made.
     *
     * JSON is the one content type excluded, because it has a better answer of its own:
     * an oversized JSON memo reaches the length rule and gets a 422 naming the field it
     * was too long for, which is more use than a sentence about recordings. That is
     * verified behaviour that bootstrap/app.php pins on purpose. Everything else --
     * multipart, form-encoded, or a body sent with no Content-Type -- is judged here,
     * because before this it answered "The text field is required when audio is not
     * present." to a 13 MB upload.
     */
    private function rejectDiscardedBody(): void
    {
        if ($this->isJson()) {
            return;
        }

        if ($this->allFiles() !== []) {
            return;
        }

        $declared = (int) $this->server('CONTENT_LENGTH');

        if ($declared > $this->maxAudioBytes()) {
            throw $this->tooLarge($declared);
        }
    }

    /**
     * 413 with a sentence somebody can act on, which is the whole acceptance criterion.
     *
     * HttpException rather than Illuminate's PostTooLargeException, so that the two
     * origins of a 413 on this route stay distinguishable: this one knows what the cap
     * is and often what was sent, and the framework's middleware knows neither.
     * bootstrap/app.php renders that one through the same message below.
     *
     * The message survives to the client with APP_DEBUG=false -- Laravel keeps an
     * HttpException's own message and replaces everything else with "Server Error" --
     * which is what makes it worth writing.
     *
     * @param  ?int  $bytes  What was sent, when that is knowable. Null for an upload PHP
     *                       rejected before keeping any of it.
     */
    private function tooLarge(?int $bytes): HttpException
    {
        return new HttpException(
            Response::HTTP_REQUEST_ENTITY_TOO_LARGE,
            self::tooLargeMessage($this->maxAudioBytes(), $bytes),
        );
    }

    /**
     * Shared with bootstrap/app.php, which renders the framework's own 413 through it so
     * that both spellings of "too large" answer in the same words.
     *
     * Both sides are formatted the same way, so the comparison in the sentence is
     * internally consistent whatever unit it lands in.
     *
     * Three sentences rather than one with a number substituted into it, and the middle
     * one is the reason. A recording a few bytes over the cap renders both sides
     * identically: "That recording is 12.0 MB. The limit is 12.0 MB" reads as a
     * contradiction rather than as a rejection, and it was answered by a live upload of
     * exactly MAX_AUDIO_BYTES + 1. No precision fixes that -- one byte over always
     * rounds to the same string as the cap -- so when the two would agree, the size is
     * dropped and the sentence says what it can say honestly instead.
     */
    public static function tooLargeMessage(int $limitBytes, ?int $bytes = null): string
    {
        $limit = self::size($limitBytes);

        // Full stops rather than a dash before the instruction, because this sentence is
        // rarely read on its own: useMemos prefixes it with "Could not upload the
        // recording — " before rendering it, and a second em-dash inside makes the banner
        // read as one clause that never ends.
        return match (true) {
            $bytes === null => "That recording is too large. The limit is {$limit}. Record a shorter memo.",
            self::size($bytes) === $limit => "That recording is just over the {$limit} limit. Record a shorter memo.",
            default => 'That recording is '.self::size($bytes).". The limit is {$limit}. Record a shorter memo.",
        };
    }

    /**
     * A byte count as something a person reads, in the largest unit that leaves a
     * non-zero number in front of it.
     *
     * Megabytes alone is what this was, and it produced "The limit is 0.0 MB" the first
     * time MAX_AUDIO_BYTES was lowered to try the rejection out -- a cap of 50,000 bytes
     * is a twentieth of a megabyte, and one decimal has nowhere to put that. Every cap
     * under about 51 KB reads as zero, which tells the reader their limit is nothing at
     * all. Sibling of the collision above and the same underlying mistake: a single fixed
     * format cannot describe a value that is configurable across four orders of
     * magnitude.
     *
     * Binary units under decimal names, which is the ordinary reading of "MB" for a file
     * and matches how the cap is written (12 MiB = 12,582,912). The README says MiB
     * because it is talking to somebody editing the number; this is talking to somebody
     * who recorded something too long.
     */
    private static function size(int $bytes): string
    {
        return match (true) {
            $bytes < 1024 => $bytes.' bytes',
            $bytes < 1024 * 1024 => number_format($bytes / 1024).' KB',
            default => number_format($bytes / 1024 / 1024, 1).' MB',
        };
    }

    /**
     * config() rather than a constant, unlike MAX_TEXT_LENGTH: this one is
     * MAX_AUDIO_BYTES in .env.example and docker-compose.yml, and GET /api/health exists
     * to compare it against the two PHP limits that have to stay above it.
     */
    private function maxAudioBytes(): int
    {
        return (int) config('memo.max_audio_bytes');
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

                // An upload PHP completed. What is left for this to catch after
                // rejectUnacceptableUpload() has run is UPLOAD_ERR_PARTIAL -- a
                // connection that dropped mid-body -- and a field that is not a file at
                // all, which would otherwise reach audio(), find no UploadedFile, return
                // null, and be stored as a text memo with no text in it.
                'file',

                // Deliberately no `max:`. The byte cap is enforced before validation so
                // that it answers 413 rather than a 422 about a form field; adding it
                // here would produce a second, quieter answer to the same question. See
                // rejectUnacceptableUpload().
                new SniffedAudioType,
            ],

            // Absent means "detect it", which is why there is no default and no `en`
            // fallback: the absence is the instruction, exactly as STT_LANGUAGE's empty
            // value is in memo_ai/config.py. `nullable` so a client that sends the field
            // empty -- a <select> whose "Auto-detect" option has value="" -- means the
            // same thing as one that omits it, since ConvertEmptyStringsToNull turns
            // that into a present null.
            //
            // Accepted on a text memo as well as a recording, and it is simply ignored
            // there: a typed memo is never transcribed, so there is no decode for it to
            // steer. Refusing the pair would mean the browser had to strip the field
            // depending on which control the user used, to no benefit.
            'language' => [
                'nullable',
                'string',
                new SupportedLanguage,
            ],
        ];
    }

    /**
     * The language this memo should be decoded in, or null to detect it.
     *
     * Null for both "absent" and "present but empty", because the browser's
     * "Auto-detect" option sends an empty value and means the same thing.
     */
    public function language(): ?string
    {
        $language = $this->validated()['language'] ?? null;

        return is_string($language) && $language !== '' ? $language : null;
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
     * Both come through SniffedAudioType now, which is what makes the value stored in
     * `memos.audio_mime` the same value the allowlist vouched for rather than a second
     * opinion about the same bytes. The `application/octet-stream` fallback below is
     * unreachable behind that rule -- a file it cannot name is a file the rule refused --
     * and is kept because this method must still be total if it is ever called from
     * somewhere the rule did not run.
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
            mimeType: SniffedAudioType::of($file) ?? 'application/octet-stream',
            extension: preg_match('/^[a-z0-9]{1,8}$/', $extension) === 1
                ? $extension
                : self::FALLBACK_EXTENSION,
        );
    }
}

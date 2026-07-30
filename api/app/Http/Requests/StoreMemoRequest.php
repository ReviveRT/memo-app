<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for POST /api/memos.
 *
 * No authorize() method, and that is not an omission: this app has no
 * authentication by design (README, "Assumptions"), and FormRequest treats an
 * absent authorize() as granted rather than denied.
 *
 * MEMO-11 adds the audio path to this same route and will have to relax `required`
 * on `text` to something like required_without:audio. Until there is a second field
 * to be exclusive with, spelling that out now would be a rule with no case that
 * exercises it.
 */
final class StoreMemoRequest extends FormRequest
{
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
     * characters is roughly 2,500 tokens: far more than anyone types into a memo box
     * and small enough that a scripted flood is a nuisance rather than a bill.
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
     * @return array<string, list<string>>
     */
    public function rules(): array
    {
        return [
            // min:1 after the trim above is what rejects "   ". Laravel counts
            // characters with mb_strlen for a string under a `string` rule, so
            // max: is multibyte-safe and an emoji costs one character rather than
            // four.
            'text' => ['required', 'string', 'min:1', 'max:'.self::MAX_TEXT_LENGTH],
        ];
    }

    /** The validated text, which is exactly what lands in `transcript`. */
    public function text(): string
    {
        return (string) $this->validated()['text'];
    }
}

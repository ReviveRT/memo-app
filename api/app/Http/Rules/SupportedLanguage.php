<?php

declare(strict_types=1);

namespace App\Http\Rules;

use Closure;
use Illuminate\Contracts\Validation\ValidationRule;

/**
 * Refuses a language code the transcriber has never heard of.
 *
 * The set is Whisper's, which is the model behind the only provider that decodes
 * anything (memo_ai/stt/local.py). Checked here rather than in the worker because the
 * two failures are not comparable: a bad code refused at the edge is a 422 with a
 * readable sentence in front of somebody who can fix it, while the same code accepted
 * onto the row is a memo that queues, gets claimed, raises inside faster-whisper and
 * lands in `failed` a poll interval later with a library's message on it.
 *
 * **Duplicated from the model rather than derived from it, and stated as a limitation.**
 * There is no endpoint that reports what the configured provider supports, so this list
 * is a copy -- the same trade `config/memo.php` makes against docker-compose.yml and
 * `memo_ai/config.py` makes against both. It goes stale in one direction only: a code
 * Whisper gains would be refused here until this file is updated, which is a 422 rather
 * than a broken memo. A provider with a *smaller* set is the one to watch, and it is why
 * `STT_PROVIDER=openai` is documented as unbuilt rather than silently accepted.
 *
 * No `en` special case and no default. Absent means "detect it", which is a different
 * thing from any code in this list and is expressed by the field being absent -- see
 * `language` in StoreMemoRequest and 005_memo_language.sql.
 */
final class SupportedLanguage implements ValidationRule
{
    /**
     * The 99 codes Whisper's tokenizer defines, ordered as its own table is.
     *
     * `yue` is here and is the one to know about: Cantonese was added in large-v3 and is
     * absent from large-v2, so it is the single entry whose validity depends on
     * `STT_MODEL`. It is accepted because the shipped default is large-v3-turbo; on an
     * older model it fails the memo rather than the request, which is the documented
     * direction for a provider capability this rule cannot see.
     *
     * @var list<string>
     */
    public const CODES = [
        'af', 'am', 'ar', 'as', 'az', 'ba', 'be', 'bg', 'bn', 'bo',
        'br', 'bs', 'ca', 'cs', 'cy', 'da', 'de', 'el', 'en', 'es',
        'et', 'eu', 'fa', 'fi', 'fo', 'fr', 'gl', 'gu', 'ha', 'haw',
        'he', 'hi', 'hr', 'ht', 'hu', 'hy', 'id', 'is', 'it', 'ja',
        'jw', 'ka', 'kk', 'km', 'kn', 'ko', 'la', 'lb', 'ln', 'lo',
        'lt', 'lv', 'mg', 'mi', 'mk', 'ml', 'mn', 'mr', 'ms', 'mt',
        'my', 'ne', 'nl', 'nn', 'no', 'oc', 'pa', 'pl', 'ps', 'pt',
        'ro', 'ru', 'sa', 'sd', 'si', 'sk', 'sl', 'sn', 'so', 'sq',
        'sr', 'su', 'sv', 'sw', 'ta', 'te', 'tg', 'th', 'tk', 'tl',
        'tr', 'tt', 'uk', 'ur', 'uz', 'vi', 'yi', 'yo', 'yue', 'zh',
    ];

    public function validate(string $attribute, mixed $value, Closure $fail): void
    {
        if (! is_string($value) || ! in_array($value, self::CODES, true)) {
            // The value is echoed back because the likely cause is a locale rather than
            // a language -- "en-GB", "ro_RO", "rus" -- and seeing it beside the word
            // "code" is what makes the shape of the mistake obvious. Not the whole list:
            // 99 codes in a validation message is not a readable sentence.
            $fail(
                'The :attribute field must be a supported two-letter language code, '
                .'such as en, ru or ro. Leave it out to detect the language automatically.'
            );
        }
    }
}

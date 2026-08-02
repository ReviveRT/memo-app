<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\SupportedLanguage;
use Illuminate\Foundation\Http\FormRequest;

/**
 * Validation for POST /api/memos/{memo}/retranscribe.
 *
 * One field, and it is optional. `language` names what to decode the recording in;
 * leaving it out re-runs the memo on auto-detect, which is the way back for somebody who
 * pinned the wrong language and would rather the model tried again on its own.
 *
 * **Why this route has a body and `retry` does not.** MEMO-17's Retry sends a memo back
 * unchanged -- there is nothing for a client to choose, so it takes no body at all. This
 * one exists precisely to change something, and the thing it changes is the only reason
 * to call it: re-running a memo with no new instruction would spend a worker to produce
 * the transcript that is already on the row.
 *
 * Same treatment of absence as StoreMemoRequest: `nullable` so that a client sending
 * `{"language": ""}` -- a <select> whose "Auto-detect" option has an empty value -- means
 * the same thing as one sending `{}`, because ConvertEmptyStringsToNull turns the first
 * into a present null and `SupportedLanguage` would otherwise refuse it.
 */
final class RetranscribeMemoRequest extends FormRequest
{
    /**
     * @return array<string, list<mixed>>
     */
    public function rules(): array
    {
        return [
            'language' => [
                'nullable',
                'string',
                new SupportedLanguage,
            ],
        ];
    }

    /**
     * The language to decode in, or null to detect it.
     *
     * Null for both "absent" and "present but empty", so the controller has one absence
     * to branch on rather than two spellings of it.
     */
    public function language(): ?string
    {
        $language = $this->validated()['language'] ?? null;

        return is_string($language) && $language !== '' ? $language : null;
    }
}

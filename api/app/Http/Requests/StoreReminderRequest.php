<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Http\Rules\NoNullBytes;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Support\Carbon;

/**
 * Validation for POST /api/memos/{memo}/reminders.
 *
 * Two fields: when, and optionally what about.
 *
 * **One field for both of the UI's controls.** The card offers an alarm -- pick a date and a
 * time -- and a notification timer -- "in 30 minutes". Those are two ways of arriving at one
 * instant, and the difference is resolved in the browser against its own clock before the
 * request is made. So there is no `mode`, no `minutes_from_now`, and nothing here that has to
 * know when "now" was: a reminder is an instant, and both controls produce one.
 *
 * That also means the API never has to reason about the user's timezone. `2026-08-02T09:00`
 * chosen in an alarm field is local, and the browser converts it before sending -- the same
 * division of labour App\Support\TimeWindow describes for the date filter.
 */
final class StoreReminderRequest extends FormRequest
{
    /**
     * "about something" -- a short line, not a second memo.
     *
     * 200 characters, matching the search caps rather than the memo cap, because that is the
     * size of thing this is: a phrase to put in a notification body. The memo it hangs off is
     * where the content lives, and a note longer than a notification can display would be
     * stored and never fully shown.
     */
    public const MAX_NOTE_LENGTH = 200;

    /**
     * How far into the past a reminder may be set, in seconds.
     *
     * Zero would be the obvious rule -- a reminder for a moment that has passed is
     * meaningless -- and it is not quite safe. The instant is computed by the *browser* and
     * judged by the *API*, so a short timer is a race against whatever the two clocks
     * disagree by: "in 60 seconds" from a browser 90 seconds behind the server arrives already
     * expired and is rejected, which reads as the timer button being broken.
     *
     * A minute of slack absorbs that while still refusing the mistake this rule is for --
     * picking yesterday's date in the alarm field, which is off by hours or days rather than
     * by seconds.
     *
     * In the shipped configuration the skew is zero: the browser is on the host and the API is
     * in a container sharing the host's clock. This is here for the case where they are not,
     * because that case fails intermittently and would be hard to recognise.
     */
    public const PAST_TOLERANCE_SECONDS = 60;

    /**
     * Trimmed before validation, so the length rule judges the string that will be stored, and
     * so a note of nothing but spaces becomes absent rather than a stored blank.
     *
     * The empty string is left for ConvertEmptyStringsToNull in the global stack to turn into
     * null, which is what `note` means when there is nothing to say -- the same collapse
     * ListMemosRequest relies on for a blank `q`.
     */
    protected function prepareForValidation(): void
    {
        $note = $this->input('note');

        if (is_string($note)) {
            $this->merge(['note' => trim($note)]);
        }
    }

    /**
     * @return array<string, mixed>
     */
    public function rules(): array
    {
        return [
            // `date` rather than `date_format:`, for the reason FiltersByTime gives about
            // `from` and `to`: the client sends what Date#toISOString produces, and pinning a
            // format would reject an equivalent offset spelling for no benefit.
            'remind_at' => [
                'required',
                'date',

                // A closure rather than `after:now`, for the tolerance above. `after:now` has
                // no way to express "not more than a minute ago", and it would also compare
                // against the moment the rule runs rather than against a value this class
                // chose -- so the slack would be invisible in the rules array.
                function (string $attribute, mixed $value, callable $fail): void {
                    if (! is_string($value)) {
                        return;
                    }

                    $at = strtotime($value);

                    // False is unreachable: the `date` rule above has already parsed this.
                    // Guarded anyway, because the alternative is comparing false against an
                    // int, where false is 0 and every reminder would be judged as 1970.
                    if ($at === false) {
                        return;
                    }

                    if ($at < time() - self::PAST_TOLERANCE_SECONDS) {
                        $fail('The :attribute field must be a time in the future.');
                    }
                },
            ],

            // NoNullBytes for the reason the rule class gives: libpq truncates a bound
            // parameter at the first NUL, so an interior one would silently store half the
            // note and answer 201 as though it had stored all of it.
            'note' => ['sometimes', 'nullable', 'string', 'max:'.self::MAX_NOTE_LENGTH, new NoNullBytes],
        ];
    }

    /**
     * Renamed for the message, because the frontend renders a failed request's `message`
     * verbatim and "The remind_at field must be a time in the future" names a JSON key rather
     * than the control the user was using.
     *
     * @return array<string, string>
     */
    public function attributes(): array
    {
        return ['remind_at' => 'reminder time'];
    }

    /**
     * The instant, normalised to UTC with an explicit offset.
     *
     * The same format App\Support\TimeWindow binds with, and for the same reason: a bare
     * timestamp bound to a `timestamptz` is resolved against the server's TimeZone setting, so
     * the identical request would mean different moments on two differently configured
     * databases. Not shared with TimeWindow as code, because that class is a half-open
     * *interval* and this is a single point -- the formatting is one line, and folding a point
     * into an interval type to save it would be the wrong abstraction.
     */
    public function remindAt(): string
    {
        return Carbon::parse((string) $this->validated()['remind_at'])
            ->utc()
            ->format('Y-m-d\TH:i:s.uP');
    }

    /** The note, or null when there is nothing to say. */
    public function note(): ?string
    {
        $note = $this->validated()['note'] ?? null;

        return is_string($note) && $note !== '' ? $note : null;
    }
}

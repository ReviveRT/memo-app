<?php

declare(strict_types=1);

namespace App\Http\Requests\Concerns;

use App\Support\TimeWindow;
use Illuminate\Contracts\Validation\Validator;

/**
 * The `from` and `to` parameters, shared by the memo list and the collection list.
 *
 * A trait rather than a base FormRequest, because the two requests have nothing else in
 * common -- one validates a limit and a text filter, the other a text filter alone -- and
 * a shared parent would exist only to hold these six lines. The thing that genuinely must
 * not diverge is what a window *means*, and that lives in App\Support\TimeWindow where
 * both of them read it from.
 *
 * The user-facing reason both lists take the same two parameters: the brief asks for one
 * filter that works the same way in both places. "Everything from last Tuesday" is the
 * same question whether it is asked of memos or of collections, and a UI offering two
 * date pickers that behaved differently would be the thing to avoid.
 */
trait FiltersByTime
{
    /**
     * Merged into the implementing request's own rules.
     *
     * `date` rather than `date_format:...`: the client sends whatever
     * `Date#toISOString` produces, and pinning a format would reject the `+02:00` offset
     * form for no benefit -- both are the same instant and TimeWindow normalises either.
     *
     * nullable for the reason `q` and `limit` are nullable in ListMemosRequest: `?from=`
     * is the ordinary output of a frontend building a query string with no date chosen,
     * and a 422 for it would mean the list could not load until a date was picked.
     *
     * Ordering is deliberately *not* expressed as `after:from` here. That rule resolves
     * its parameter by looking up the other field and falling back to treating the
     * literal string `from` as a date when the field is absent -- so a request sending
     * only `to` depends on how a failed strtotime is handled rather than on anything this
     * class decided. The check is done explicitly in withValidator() below, where the
     * absent-`from` case is visible.
     *
     * @return array<string, list<string>>
     */
    protected function timeWindowRules(): array
    {
        return [
            'from' => ['sometimes', 'nullable', 'date'],
            'to' => ['sometimes', 'nullable', 'date'],
        ];
    }

    /**
     * Refuses a window that cannot contain anything.
     *
     * Strictly after, not `after_or_equal`, and that follows from the interval being
     * half-open: `from == to` is the empty set. A request for it is answerable -- zero
     * rows -- and it is not a question anybody means to ask, so it is worth a 422 that
     * names the problem rather than an empty list that reads as "you have no memos". The
     * likeliest way to produce one is a date picker that forgot to advance `to` by a day
     * for a single-day range, and an empty list is exactly how that bug hides.
     *
     * Only checked when both ends are present, which is what the two `isset` guards are
     * for -- one end alone is a perfectly good window and has no ordering to violate.
     *
     * Both strings have already passed the `date` rule by the time an after() callback
     * runs, so strtotime cannot fail here. Compared as timestamps rather than as strings,
     * because `2026-07-19T00:00:00+02:00` sorts after `2026-07-19T00:00:00Z` as text
     * while being the earlier instant.
     */
    public function withValidator(Validator $validator): void
    {
        $validator->after(function (Validator $validator): void {
            /** @var array<string, mixed> $data */
            $data = $validator->getData();

            $from = $data['from'] ?? null;
            $to = $data['to'] ?? null;

            if (! is_string($from) || ! is_string($to) || $from === '' || $to === '') {
                return;
            }

            if ($validator->errors()->hasAny(['from', 'to'])) {
                return;
            }

            if (strtotime($to) <= strtotime($from)) {
                $validator->errors()->add(
                    'to',
                    'The end of the date range must be after the start.',
                );
            }
        });
    }

    /**
     * The validated window, or an unbounded one when neither end was given.
     *
     * Both spellings of "no date filter" -- the parameter missing, and the parameter
     * present but blank -- collapse inside TimeWindow rather than here, so no repository
     * has to decide whether '' is a bound.
     */
    public function timeWindow(): TimeWindow
    {
        $validated = $this->validated();

        $from = $validated['from'] ?? null;
        $to = $validated['to'] ?? null;

        return TimeWindow::between(
            is_string($from) ? $from : null,
            is_string($to) ? $to : null,
        );
    }

    /**
     * `from` and `to` renamed for the 422, because the frontend renders a failed GET's
     * `message` verbatim (web/src/api/memos.js) and "The from field is not a valid date"
     * describes a query-string parameter the person filtering never typed.
     *
     * ListMemosRequest merges this into its own attributes(); see the note there about
     * why the merge is spelled out rather than inherited.
     *
     * @return array<string, string>
     */
    protected function timeWindowAttributes(): array
    {
        return [
            'from' => 'start of the date range',
            'to' => 'end of the date range',
        ];
    }
}

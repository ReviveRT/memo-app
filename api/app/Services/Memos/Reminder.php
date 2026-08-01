<?php

declare(strict_types=1);

namespace App\Services\Memos;

use RuntimeException;

/**
 * One reminder, as the API hands it out.
 *
 * Reminders never travel alone: they arrive nested inside the memo they are about, in
 * every response that carries a memo. That is deliberate and is the reason there is no
 * GET /api/reminders -- the list has to badge a memo that has something pending, so the
 * reminders have to be on the row already or the strip would need a second request per
 * memo to know whether to draw a bell.
 *
 * Built from a decoded jsonb object rather than from a database row, because that is how
 * it reaches PHP: MemoRepository::COLUMNS aggregates the reminders for a memo into one
 * jsonb array in SQL, so what arrives here is an array from json_decode and not a
 * stdClass from the driver. Memo::fromRow is the other half of that.
 */
final class Reminder
{
    /**
     * @param  string  $remindAt  Already ISO-8601 in UTC; the aggregate in
     *                            MemoRepository::COLUMNS formats it with the same
     *                            to_char expression the memo's own created_at uses, so
     *                            every timestamp this API emits has one format.
     * @param  ?string  $note  What the user wanted reminding about, or null when the memo
     *                         is the answer.
     * @param  ?string  $deliveredAt  When it was actually shown, or null for "still owed".
     */
    public function __construct(
        public readonly string $id,
        public readonly string $remindAt,
        public readonly ?string $note,
        public readonly ?string $deliveredAt,
    ) {}

    /**
     * Maps one element of the memo's `reminders` array.
     *
     * @param  array<string, mixed>  $row
     *
     * @throws RuntimeException When the object is missing a key this expects, which means
     *                          the jsonb_build_object in MemoRepository::COLUMNS and this
     *                          method disagree. Loud for the same reason Memo::fromRow is
     *                          loud about a missing column: reading an absent key would
     *                          otherwise ship `"id": ""` and a reminder that can never be
     *                          acknowledged or deleted, because nothing can name it.
     */
    public static function fromJson(array $row): self
    {
        foreach (['id', 'remind_at', 'note', 'delivered_at'] as $key) {
            if (! array_key_exists($key, $row)) {
                throw new RuntimeException(
                    "Reminder object is missing the key {$key}: MemoRepository::COLUMNS and Reminder::fromJson disagree."
                );
            }
        }

        return new self(
            id: (string) $row['id'],
            remindAt: (string) $row['remind_at'],
            note: $row['note'] === null ? null : (string) $row['note'],
            deliveredAt: $row['delivered_at'] === null ? null : (string) $row['delivered_at'],
        );
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return [
            'id' => $this->id,
            'remind_at' => $this->remindAt,
            'note' => $this->note,

            // Null is the value the frontend acts on -- it is what "this one has not fired
            // yet" is spelled as -- so the key is always present rather than omitted when
            // absent. See web/src/composables/useReminders.js.
            'delivered_at' => $this->deliveredAt,
        ];
    }
}

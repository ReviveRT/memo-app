<?php

declare(strict_types=1);

namespace App\Services\Memos;

use JsonException;
use RuntimeException;
use stdClass;

/**
 * One memo, as the API hands it out.
 *
 * This is the whole response contract for both routes: POST returns one of these
 * and GET returns a list of them, so a field the list shows and the create
 * response omits cannot exist. That mattered enough to be worth a single class --
 * MEMO-18 polls GET and replaces rows keyed by id, and it can only do that if the
 * object it optimistically inserted after a POST has the same shape as the one the
 * poll brings back.
 *
 * Deliberately not the whole row. `search_vector` is the largest column on the
 * table and is never sent; the queue bookkeeping (attempts, locked_at,
 * next_attempt_at) is the worker's business and no client renders it; and
 * `enrichment_error` is separate from `last_error` on purpose (a failed enrichment
 * still reaches 'ready'). Since MEMO-21 there is an enricher that can fail, so that
 * column now carries sentences -- and a client rendering it must not treat one as a
 * failed memo, because the transcript is there.
 */
final class Memo
{
    /** The two values the source CHECK constraint allows. */
    public const SOURCE_TEXT = 'text';

    /**
     * A memo that arrived as a recording (MEMO-10).
     *
     * The distinction it draws is not "has audio" -- it is which of `transcript` and
     * `audio_path` was set at INSERT time, and therefore what the worker owes the row.
     * A voice memo starts with a NULL transcript and gets one; a text memo brings its
     * own. `source` is what survives that difference once both rows look alike.
     */
    public const SOURCE_VOICE = 'voice';

    /**
     * Where every memo starts, audio or text.
     *
     * A text memo is 'queued' rather than 'ready' because the worker still owes it
     * an enrichment pass -- a title, a summary and tags. It branches on
     * `transcript IS NULL` to decide whether transcription is owed too, so the
     * typed text arriving already set is what sends this row straight to
     * enrichment. One predicate, one queue, no second status column.
     */
    public const STATUS_QUEUED = 'queued';

    /** Claimed by a worker replica. The other half of "still owes work". */
    public const STATUS_PROCESSING = 'processing';

    /**
     * Every value 001_init.sql's CHECK constraint allows, in lifecycle order.
     *
     * @var list<string>
     */
    public const STATUSES = [self::STATUS_QUEUED, self::STATUS_PROCESSING, 'ready', 'failed'];

    /**
     * The statuses nothing further happens to.
     *
     * Stated as the terminal set so inFlightStatuses() below can be everything else, which
     * is the direction MEMO-18 argues for on the frontend and it is the same argument here:
     * a positive list of unfinished statuses is one value short the moment a status is
     * added, and MEMO-16 adds a retry path. Getting that wrong is quiet -- a memo in a
     * status this file has never heard of would simply stop being pinned, and would vanish
     * from a filtered list while it was still being worked on. Unknown means not finished.
     *
     * @var list<string>
     */
    public const TERMINAL_STATUSES = ['ready', 'failed'];

    /**
     * The statuses that mean a memo is not finished with: every allowed value that is not
     * terminal. Today that is 'queued' and 'processing'.
     *
     * Derived rather than listed, so adding a status to STATUSES pins it by default and
     * only an explicit entry in TERMINAL_STATUSES stops that. One edit, not two, and the
     * edit that is easy to forget is the safe one to forget.
     *
     * Why the search pins these at all: a memo still being transcribed has no transcript,
     * so it matches no query and would drop off the list while a filter was active --
     * immediately after the user recorded it. See MemoRepository::search.
     *
     * 'failed' is terminal, so it is deliberately not pinned. It is also unmatchable
     * (transcription failed, so there is nothing to match), but it is a settled outcome
     * rather than a gap in the pipeline, and pinning it would put every past failure at
     * the top of every search. MEMO-17 owns surfacing those.
     *
     * A method rather than a constant, only because a constant expression cannot call
     * array_diff. The array_values is not cosmetic: array_diff preserves the original keys,
     * and MemoRepository::search spreads this into positional bindings, where a gapped
     * array would bind in an order nobody intended.
     *
     * @return list<string>
     */
    public static function inFlightStatuses(): array
    {
        return array_values(array_diff(self::STATUSES, self::TERMINAL_STATUSES));
    }

    /**
     * Every column fromRow() needs, which is every output name in
     * MemoRepository::COLUMNS. The two have to agree, and this is what says so out
     * loud instead of letting a renamed column become an empty string in a response.
     */
    private const REQUIRED_COLUMNS = [
        'id', 'source', 'status', 'transcript', 'title',
        'summary', 'tags', 'category', 'duration_ms', 'last_error',
        'last_error_code', 'language', 'created_at_iso', 'collection_id',
        'reminders',
    ];

    /**
     * @param  list<string>  $tags
     * @param  string  $createdAt  Already ISO-8601 in UTC; the repository asks Postgres
     *                             to format it. See MemoRepository::COLUMNS.
     * @param  ?string  $collectionId  The collection this memo is filed in, or null for a
     *                                 fast memo. Null is a state rather than missing data:
     *                                 it is what puts the memo in the strip at the top of
     *                                 the page, and 003_collections_and_reminders.sql has
     *                                 the argument for one column instead of a join table.
     * @param  list<Reminder>  $reminders  Soonest first. Empty for most memos, and always
     *                                     an array -- the aggregate in
     *                                     MemoRepository::COLUMNS coalesces to `[]` so no
     *                                     reader needs a null check before iterating.
     */
    public function __construct(
        public readonly string $id,
        public readonly string $source,
        public readonly string $status,
        public readonly ?string $transcript,
        public readonly ?string $title,
        public readonly ?string $summary,
        public readonly array $tags,

        /**
         * What kind of thing this memo is -- 'task', 'idea' or 'note' -- or null for a
         * memo no enrichment pass has classified.
         *
         * Null on every row today: MEMO-21 owns the enricher and nothing writes the
         * column until it lands. It is on the wire regardless, for the same reason
         * `summary` is -- both are enrichment output, and a client that renders one when
         * it is present renders the other the same way.
         *
         * Not validated against those three values, and there is no CHECK constraint
         * behind the column either -- unlike `source` and `status`, which have one each.
         * The vocabulary is the enricher's, and a fourth category invented by a worker
         * newer than this API should reach the reader as itself rather than as an error
         * -- the same argument `lastErrorCode` makes below.
         */
        public readonly ?string $category,

        public readonly ?int $durationMs,
        public readonly ?string $lastError,

        /**
         * Which *kind* of failure `lastError` describes, or null for a memo that has
         * never failed.
         *
         * A short token from a closed vocabulary -- memo_ai/failures.py owns it -- and
         * it is on the wire beside the sentence because the two answer different
         * questions. The sentence is what a person reads; this is what a program
         * branches on, and the frontend has one branch that matters: a recording with
         * nothing in it is discarded rather than shown, and everything else is kept
         * with a Retry. Deriving that from the sentence would mean matching on prose
         * that exists to be reworded. 004_last_error_code.sql has the argument.
         *
         * Not validated here against a list of known codes. This class maps a row; a
         * code it has never heard of is a worker newer than this API, and the honest
         * thing is to pass it through and let the reader treat an unknown kind as "some
         * other failure" -- which is the direction that keeps a memo rather than
         * deleting it.
         */
        public readonly ?string $lastErrorCode,

        public readonly string $createdAt,

        /**
         * The language this memo is decoded in, or null for "detect it".
         *
         * Carried to the browser with the rest of the row. The worker reads the column
         * too, but through its own claim projection rather than this DTO -- see
         * `_CLAIM_COLUMNS` in memo_ai/memos.py.
         *
         * Nothing in the UI renders it today, so this is a value the browser is handed
         * and does not use. It had one reader: a "Spoken language" select in MemoDialog
         * that showed what a memo was decoded as and re-decoded it on change. That went
         * when a wrong transcript became something you correct by editing rather than
         * re-run the model on -- web/src/languages.js has the rest.
         *
         * Defaulted, and placed here rather than beside `lastErrorCode` where it belongs
         * by subject, for one uninteresting reason: PHP deprecates an optional parameter
         * standing before a required one, and `createdAt` is required. The default is not
         * a relaxation of the projection contract -- that is REQUIRED_COLUMNS plus the
         * property_exists loop in fromRow, which names the missing column and is asserted
         * by test_a_projection_that_no_longer_matches_the_dto_fails_immediately. Nothing
         * reaching this class from a database row can skip the argument.
         */
        public readonly ?string $language = null,

        public readonly ?string $collectionId = null,
        public readonly array $reminders = [],
    ) {}

    /**
     * Maps one row of MemoRepository::COLUMNS. Laravel's Connection::select hands
     * back stdClass, not arrays.
     *
     * @throws JsonException When `tags` is not the JSON the query asked Postgres for.
     *                       Loud on purpose: the alternative to throwing is
     *                       json_decode returning null and a memo quietly losing
     *                       every tag it had.
     * @throws RuntimeException When the row is missing a column this expects. Reading
     *                          an absent property off stdClass is a warning and a
     *                          null, so without this a projection edited on one side
     *                          of the seam ships `"id": ""` instead of failing.
     */
    public static function fromRow(stdClass $row): self
    {
        foreach (self::REQUIRED_COLUMNS as $column) {
            if (! property_exists($row, $column)) {
                throw new RuntimeException(
                    "Memo row is missing the column {$column}: MemoRepository::COLUMNS and Memo::fromRow disagree."
                );
            }
        }

        return new self(
            id: (string) $row->id,
            source: (string) $row->source,
            status: (string) $row->status,
            transcript: self::nullableString($row->transcript),
            title: self::nullableString($row->title),
            summary: self::nullableString($row->summary),
            tags: self::tags($row->tags),
            category: self::nullableString($row->category),

            // pdo_pgsql has returned native ints for integer columns since PHP 8.1,
            // so this cast is normally a no-op -- but duration_ms is nullable and
            // (int) null is 0, which would report a memo with no known duration as
            // one lasting zero milliseconds. The null check is the part that matters.
            durationMs: $row->duration_ms === null ? null : (int) $row->duration_ms,

            lastError: self::nullableString($row->last_error),
            lastErrorCode: self::nullableString($row->last_error_code),
            language: self::nullableString($row->language),
            createdAt: (string) $row->created_at_iso,
            collectionId: self::nullableString($row->collection_id),
            reminders: self::reminders($row->reminders),
        );
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return [
            'id' => $this->id,
            'source' => $this->source,
            'status' => $this->status,
            'transcript' => $this->transcript,
            'title' => $this->title,
            'summary' => $this->summary,
            'tags' => $this->tags,
            'category' => $this->category,
            'duration_ms' => $this->durationMs,
            'last_error' => $this->lastError,
            'last_error_code' => $this->lastErrorCode,
            'language' => $this->language,
            'created_at' => $this->createdAt,
            'collection_id' => $this->collectionId,
            'reminders' => array_map(
                static fn (Reminder $reminder): array => $reminder->toArray(),
                $this->reminders,
            ),
        ];
    }

    /**
     * The column comes back as JSON rather than as Postgres' own `{a,b}` array
     * literal, because that literal is not the trivial split it looks like: an
     * element containing a comma, a brace or a quote arrives quoted and
     * backslash-escaped, and MEMO-21 generates these tags from model output. The
     * query does the conversion with to_jsonb() so the escaping is Postgres'
     * problem rather than a regex here.
     *
     * @return list<string>
     *
     * @throws JsonException
     */
    private static function tags(mixed $value): array
    {
        if (! is_string($value)) {
            return [];
        }

        /** @var mixed $decoded */
        $decoded = json_decode($value, true, flags: JSON_THROW_ON_ERROR);

        if (! is_array($decoded)) {
            return [];
        }

        // array_values, so a JSON object -- which decodes to a string-keyed array --
        // cannot turn the response's `tags` from a JSON array into a JSON object and
        // break a frontend iterating it.
        return array_values(array_map(
            static fn (mixed $tag): string => is_scalar($tag) ? (string) $tag : '',
            $decoded,
        ));
    }

    /**
     * The reminders aggregate, decoded.
     *
     * Arrives as a jsonb array built by MemoRepository::COLUMNS rather than as separate
     * rows, because a memo and its reminders come back from one statement -- the list
     * badges a memo that has something pending, so every row needs them. That is the same
     * reason `tags` above is jsonb: assembling it in SQL means PHP never has to know
     * Postgres' quoting rules.
     *
     * A non-array element is skipped rather than coerced. `tags` turns a non-scalar into
     * an empty string because a tag is a string and an empty one is harmless; a reminder
     * is an object with an id that has to be usable to acknowledge or delete it, and there
     * is no empty reminder to fall back to. Reminder::fromJson is the loud half of this
     * -- an object present but missing a key throws rather than being skipped, because
     * that means the aggregate and the mapper disagree.
     *
     * @return list<Reminder>
     *
     * @throws JsonException
     */
    private static function reminders(mixed $value): array
    {
        if (! is_string($value)) {
            return [];
        }

        /** @var mixed $decoded */
        $decoded = json_decode($value, true, flags: JSON_THROW_ON_ERROR);

        if (! is_array($decoded)) {
            return [];
        }

        $reminders = [];

        foreach ($decoded as $row) {
            if (is_array($row)) {
                /** @var array<string, mixed> $row */
                $reminders[] = Reminder::fromJson($row);
            }
        }

        return $reminders;
    }

    private static function nullableString(mixed $value): ?string
    {
        return $value === null ? null : (string) $value;
    }
}

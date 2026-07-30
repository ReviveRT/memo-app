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
 * still reaches 'ready'), which is a distinction MEMO-21 owns and can surface when
 * there is something to surface.
 */
final class Memo
{
    /** The two values the source CHECK constraint allows. MEMO-11 writes the other one. */
    public const SOURCE_TEXT = 'text';

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

    /**
     * Every column fromRow() needs, which is every output name in
     * MemoRepository::COLUMNS. The two have to agree, and this is what says so out
     * loud instead of letting a renamed column become an empty string in a response.
     */
    private const REQUIRED_COLUMNS = [
        'id', 'source', 'status', 'transcript', 'title',
        'summary', 'tags', 'duration_ms', 'last_error', 'created_at_iso',
    ];

    /**
     * @param  list<string>  $tags
     * @param  string  $createdAt  Already ISO-8601 in UTC; the repository asks Postgres
     *                             to format it. See MemoRepository::COLUMNS.
     */
    public function __construct(
        public readonly string $id,
        public readonly string $source,
        public readonly string $status,
        public readonly ?string $transcript,
        public readonly ?string $title,
        public readonly ?string $summary,
        public readonly array $tags,
        public readonly ?int $durationMs,
        public readonly ?string $lastError,
        public readonly string $createdAt,
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

            // pdo_pgsql has returned native ints for integer columns since PHP 8.1,
            // so this cast is normally a no-op -- but duration_ms is nullable and
            // (int) null is 0, which would report a memo with no known duration as
            // one lasting zero milliseconds. The null check is the part that matters.
            durationMs: $row->duration_ms === null ? null : (int) $row->duration_ms,

            lastError: self::nullableString($row->last_error),
            createdAt: (string) $row->created_at_iso,
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
            'duration_ms' => $this->durationMs,
            'last_error' => $this->lastError,
            'created_at' => $this->createdAt,
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

    private static function nullableString(mixed $value): ?string
    {
        return $value === null ? null : (string) $value;
    }
}

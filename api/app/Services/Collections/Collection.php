<?php

declare(strict_types=1);

namespace App\Services\Collections;

use JsonException;
use RuntimeException;
use stdClass;

/**
 * One collection, as the API hands it out.
 *
 * The same rule Memo follows and for the same reason: this is the whole response contract
 * for every collection route, so a field the list shows and the create response omits
 * cannot exist. The frontend prepends a newly created collection to the grid it is already
 * rendering, which only works if the object POST returns has the shape GET returns.
 *
 * Named `Collection` inside App\Services\Collections rather than something like
 * `MemoCollection`, and the collision worth knowing about is
 * Illuminate\Support\Collection: any file using both has to alias one of them. Nothing
 * here does -- this project has no Eloquent and does not use Illuminate collections
 * anywhere (MEMO-05) -- and naming the domain object after the domain is worth more than
 * pre-emptively avoiding a clash with a class the codebase never imports.
 *
 * `memoCount` and `recentLabels` are not columns. They are what the card in the grid draws
 * -- how full the collection is, and a glimpse of what is in it -- and they are computed
 * by the same statement that reads the row so that rendering the grid is one query rather
 * than one per card.
 */
final class Collection
{
    /**
     * Every output name CollectionRepository::COLUMNS produces. The two have to agree, and
     * this is what says so out loud instead of letting a renamed column reach the client
     * as an empty string.
     */
    private const REQUIRED_COLUMNS = ['id', 'name', 'memo_count', 'recent_labels', 'created_at_iso'];

    /**
     * @param  int  $memoCount  How many memos are filed here. Zero is normal and is what a
     *                          collection looks like between being created and being used.
     * @param  list<string>  $recentLabels  Up to a few of the newest memos' brief labels,
     *                                      newest first -- what the card lists under the
     *                                      name. See CollectionRepository::COLUMNS for how
     *                                      a label is chosen for a memo that has no title
     *                                      yet.
     * @param  string  $createdAt  Already ISO-8601 in UTC; the repository asks Postgres to
     *                             format it, the same as Memo.
     */
    public function __construct(
        public readonly string $id,
        public readonly string $name,
        public readonly int $memoCount,
        public readonly array $recentLabels,
        public readonly string $createdAt,
    ) {}

    /**
     * @throws JsonException When `recent_labels` is not the JSON the query asked Postgres
     *                       for. Loud on purpose, the same as Memo::tags: the alternative
     *                       is json_decode returning null and a card quietly showing an
     *                       empty collection.
     * @throws RuntimeException When the row is missing a column this expects.
     */
    public static function fromRow(stdClass $row): self
    {
        foreach (self::REQUIRED_COLUMNS as $column) {
            if (! property_exists($row, $column)) {
                throw new RuntimeException(
                    "Collection row is missing the column {$column}: CollectionRepository::COLUMNS and Collection::fromRow disagree."
                );
            }
        }

        return new self(
            id: (string) $row->id,
            name: (string) $row->name,

            // count(*) comes back as a bigint, which pdo_pgsql hands over as a string
            // rather than an int -- unlike a plain `integer` column. Cast rather than
            // trusted, so the JSON carries a number and the frontend's `count === 0` test
            // is not comparing against "0".
            memoCount: (int) $row->memo_count,

            recentLabels: self::labels($row->recent_labels),
            createdAt: (string) $row->created_at_iso,
        );
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return [
            'id' => $this->id,
            'name' => $this->name,
            'memo_count' => $this->memoCount,
            'recent_labels' => $this->recentLabels,
            'created_at' => $this->createdAt,
        ];
    }

    /**
     * The labels come back as a jsonb array rather than as a Postgres text[] literal, for
     * the reason Memo::tags gives: that literal quotes and backslash-escapes any element
     * containing a comma, a brace or a quote, and these are slices of somebody's
     * transcript, so every one of those characters is likely. jsonb_agg does the escaping
     * on Postgres' side.
     *
     * @return list<string>
     *
     * @throws JsonException
     */
    private static function labels(mixed $value): array
    {
        if (! is_string($value)) {
            return [];
        }

        /** @var mixed $decoded */
        $decoded = json_decode($value, true, flags: JSON_THROW_ON_ERROR);

        if (! is_array($decoded)) {
            return [];
        }

        // array_values for the reason Memo::tags uses it: a JSON object decodes to a
        // string-keyed array and would turn this field from a JSON array into a JSON
        // object, breaking a frontend iterating it.
        return array_values(array_map(
            static fn (mixed $label): string => is_scalar($label) ? (string) $label : '',
            $decoded,
        ));
    }
}

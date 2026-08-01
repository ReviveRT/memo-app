<?php

declare(strict_types=1);

namespace App\Repositories;

use App\Services\Collections\Collection;
use App\Support\TimeWindow;
use Illuminate\Database\DatabaseManager;
use Illuminate\Database\QueryException;
use RuntimeException;
use stdClass;

/**
 * SQL for the collections table, and for the two things a collection card has to say that
 * are not columns on it: how many memos it holds, and what the newest few of them are.
 *
 * Not final, for the reason MemoRepository is not final: the feature suite substitutes a
 * fake, because `php artisan test` runs against sqlite in memory (phpunit.xml) and every
 * statement here is Postgres-specific -- jsonb_agg with an ordered aggregate, to_char, a
 * correlated LATERAL-shaped subquery, `INSERT ... RETURNING` against a partial expression
 * index.
 */
class CollectionRepository
{
    /**
     * SQLSTATE for unique_violation.
     *
     * Raised by collections_name_key, the unique index over `lower(btrim(name))` from
     * 003_collections_and_reminders.sql. Turned into a 422 rather than allowed to become a
     * 500 -- see insert().
     */
    private const UNIQUE_VIOLATION = '23505';

    /**
     * The projection, shared by every statement here so the create response, the rename
     * response and the grid rows cannot drift apart. Collection::fromRow names the same
     * columns from its side.
     *
     * `to_char` on created_at for the reason MemoRepository::COLUMNS gives -- one fixed,
     * unambiguous wire format instead of whatever DateStyle and the session TimeZone
     * produce -- and aliased `created_at_iso` for the same load-bearing reason: ORDER BY
     * resolves a bare name against the output labels first, so aliasing it `created_at`
     * would silently make `ORDER BY created_at DESC` sort the formatted string and lose
     * collections_created_idx.
     *
     * **memo_count** is a correlated count rather than a denormalised column on the table.
     * A counter column would have to be maintained by every writer of `memos.collection_id`
     * -- which today is one UPDATE, and tomorrow is also `ON DELETE SET NULL` firing inside
     * Postgres where no PHP runs. That last one is what settles it: deleting a collection
     * moves its memos in a way no application code observes, so a cached count would go
     * wrong without anything having run.
     *
     * **recent_labels** picks the best short thing each memo has to identify itself by.
     * coalesce in that order because it is the order they become available: `title` and
     * `summary` are written by the enrichment pass (MEMO-21) and are null until it runs, so
     * a memo filed seconds after being recorded has only its transcript -- and a voice memo
     * has not even that until it is transcribed, which is what the final fallback covers.
     * Without it the label would be SQL NULL, jsonb_agg would put a JSON `null` in the
     * array, and the card would render a blank line.
     *
     * `left(transcript, 80)` rather than the whole thing: this is a label on a card, and the
     * transcript can be thousands of characters. Cutting in SQL rather than in PHP or CSS
     * keeps those characters off the wire -- a grid of 12 collections would otherwise carry
     * 36 full transcripts to render 36 single lines.
     *
     * The ORDER BY inside jsonb_agg is what makes "recent" true. Without it the aggregate's
     * input order is whatever the subquery's plan produced, which is not the LIMIT's order
     * and is not stable.
     *
     * `LIMIT 3` is a literal rather than a constant spliced in, and that is a constraint of
     * the syntax rather than a preference: this is a nowdoc, so nothing interpolates, and a
     * class constant cannot be concatenated into the middle of one without breaking the SQL
     * into three pieces around it. Three is what the card has room for beneath the name
     * before it stops being a glimpse and becomes a list; it appears exactly once, here.
     *
     * **Every column is qualified `c.`, and every statement below therefore aliases the
     * table `c` -- the INSERT and the UPDATE included.** The qualification is not
     * decoration: both subqueries correlate against the outer row and have `memos m` in
     * scope, so an unqualified `id` in `WHERE m.collection_id = id` would resolve to `m.id`
     * and match nothing at all, silently. `INSERT INTO collections AS c` and
     * `UPDATE collections AS c` are what let one constant serve all three statements --
     * Postgres accepts an alias on both, and without it `RETURNING c.id` fails outright
     * with `missing FROM-clause entry for table "c"`. All three were run against a real
     * Postgres, because a projection that resolves in a SELECT and not in a RETURNING is
     * exactly the kind of thing that otherwise ships half-working.
     */
    private const COLUMNS = <<<'SQL'
        c.id,
        c.name,
        to_char(c.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS created_at_iso,
        (SELECT count(*) FROM memos m WHERE m.collection_id = c.id) AS memo_count,
        (
            SELECT coalesce(jsonb_agg(recent.label ORDER BY recent.created_at DESC), '[]'::jsonb)
            FROM (
                SELECT
                    coalesce(m.title, m.summary, left(m.transcript, 80), 'Untitled memo') AS label,
                    m.created_at
                FROM memos m
                WHERE m.collection_id = c.id
                ORDER BY m.created_at DESC
                LIMIT 3
            ) recent
        ) AS recent_labels
        SQL;

    public function __construct(private readonly DatabaseManager $db) {}

    /**
     * The grid, newest first, narrowed by a name-or-contents search and a date window.
     *
     * **The search matches the collection's name or any memo inside it,** which is more than
     * "the same search box in two places" and is the point. The brief asks for the same
     * search functionality over collections as over memos, and for a folder the useful
     * reading of that is not only its name: "which collection has the memo about the
     * dentist?" is the question somebody actually has, and a name-only search cannot answer
     * it. So the predicate is an ILIKE on the name OR an EXISTS over the memos filed there,
     * reusing the same tsvector-plus-ILIKE pair the memo list uses -- one definition of what
     * "matches" means, applied to both.
     *
     * `MemoRepository::likePattern` is reused for the name rather than reimplemented, so a
     * collection called "50% margin" is searchable by `50%` for the same reason a memo is.
     * Without it the pattern's trailing `%%` reads as "anything" and every collection
     * matches.
     *
     * **The date window bounds the collection's own created_at,** not its memos'. That is the
     * reading that keeps the filter meaning one thing in both places -- "things made in this
     * range" -- and it is the one a shared date picker can describe honestly. The other
     * reading, "collections containing a memo from this range", is defensible and was not
     * chosen: combined with the search arm above it would make a card appear because of a
     * memo matching the text and a *different* memo matching the dates, which is a result
     * nobody can account for by looking at it.
     *
     * **The plan for that EXISTS is not fixed, and the cheap-looking case is not the one you
     * would guess.** Measured on 20,000 memos with a query matching nothing, which is the
     * worst input because no arm can short-circuit:
     *
     *   * **60 collections** -> `hashed SubPlan`: memos is scanned **once** for the whole
     *     request, and the result is hashed and probed per collection. 20,000 rows filtered,
     *     ~11 ms.
     *   * **8 collections** -> a correlated `SubPlan`: memos is scanned **once per
     *     collection**, `loops=8`, 20,000 rows filtered each time.
     *
     * So *fewer* collections is the slower shape, which is backwards from the intuition and
     * is why it is written down. It follows from the planner's own arithmetic -- with few
     * outer rows, re-running the correlated scan is estimated cheaper than building a hash --
     * and it means the cost is bounded by (collections x memos) rather than by memos alone.
     *
     * This was first documented as "flattened into a hashed SubPlan" on the strength of one
     * observation. That was a measurement of one dataset written up as if it were the shape.
     *
     * The tsvector and trigram indexes are not used inside the subplan in either case: the
     * planner prefers a sequential filter to index lookups for a set it is going to scan
     * wholesale anyway. Rewriting the EXISTS as an uncorrelated `c.id IN (SELECT ...)`, which
     * *forces* the single-pass shape, was tried and measured against this same fixture: 10.6 ms
     * against 11.6 ms, identical results on every probe. Not worth the churn at this size --
     * but it is the change to make if this ever holds enough collections to matter, and it is
     * a rewrite of one predicate.
     *
     * @return list<Collection>
     */
    public function list(?string $text, TimeWindow $window, int $limit): array
    {
        /** @var list<string> $where */
        $where = [];

        /** @var list<mixed> $bindings */
        $bindings = [];

        if ($text !== null) {
            $where[] = '(c.name ILIKE ?'
                .' OR EXISTS ('
                .'SELECT 1 FROM memos m'
                .' WHERE m.collection_id = c.id'
                ."   AND (m.search_vector @@ websearch_to_tsquery('english', ?)"
                .'        OR m.transcript ILIKE ?)'
                .'))';

            $pattern = MemoRepository::likePattern($text);

            $bindings[] = $pattern;
            $bindings[] = $text;
            $bindings[] = $pattern;
        }

        // Half-open, the same as the memo list -- two predicates rather than a BETWEEN. See
        // App\Support\TimeWindow.
        if ($window->from !== null) {
            $where[] = 'c.created_at >= ?';
            $bindings[] = $window->from;
        }

        if ($window->to !== null) {
            $where[] = 'c.created_at < ?';
            $bindings[] = $window->to;
        }

        $bindings[] = $limit;

        $rows = $this->db->connection()->select(
            'SELECT '.self::COLUMNS
                ."\nFROM collections c"
                .($where === [] ? '' : "\nWHERE ".implode("\n  AND ", $where))
                ."\nORDER BY c.created_at DESC"
                ."\nLIMIT ?",
            $bindings,
        );

        return array_values(array_map(
            static fn (stdClass $row): Collection => Collection::fromRow($row),
            $rows,
        ));
    }

    /**
     * Create one collection, and hand back the row that was written.
     *
     * RETURNING for the reason MemoRepository::insert uses it: `created_at` is a column
     * default, so without it the response would either omit the field or carry a timestamp
     * PHP invented. The count and labels in COLUMNS come back as 0 and `[]`, which is what
     * a new collection is.
     *
     * Returns false for a duplicate name rather than throwing, because a name that is already
     * taken is the user's ordinary mistake and belongs in a 422 next to the field they typed
     * -- not on stderr as a 500. The controller turns false into that message.
     *
     * False rather than null, matching rename() below: across this class `false` always means
     * "that name is taken" and `null` always means "no such collection". Creating has no
     * not-found case, so it only ever returns one of the two -- but using null here would
     * make the same value mean different things in two neighbouring methods, which is the
     * kind of asymmetry a caller gets wrong once and then cannot see.
     *
     * The check is the unique index rather than a SELECT first, and that is the difference
     * between "usually right" and right: two requests can both find the name free and both
     * insert it. Postgres serialises them; a check in PHP cannot.
     *
     * @param  string  $name  Already trimmed and non-blank -- SaveCollectionRequest does
     *                        that, and the CHECK constraint in
     *                        003_collections_and_reminders.sql is the backstop.
     */
    public function insert(string $id, string $name): Collection|false
    {
        try {
            $rows = $this->db->connection()->selectFromWriteConnection(
                'INSERT INTO collections AS c (id, name) VALUES (?, ?) RETURNING '.self::COLUMNS,
                [$id, $name],
            );
        } catch (QueryException $e) {
            if (! MemoRepository::isSqlState($e, self::UNIQUE_VIOLATION)) {
                throw $e;
            }

            return false;
        }

        $row = $rows[0] ?? null;

        if (! $row instanceof stdClass) {
            // Unreachable via Postgres, for the reason MemoRepository::insert gives about
            // its own equivalent: an INSERT ... RETURNING that inserts a row returns it,
            // and anything that stopped the insert raises instead. Asserted so that a
            // projection edited on one side of the seam fails here rather than as a
            // TypeError inside Collection::fromRow.
            throw new RuntimeException("INSERT of collection {$id} returned no row.");
        }

        return Collection::fromRow($row);
    }

    /**
     * Rename one collection.
     *
     * Three outcomes, and they have to stay distinguishable because the API answers
     * differently for each: the row in its new state, null for "no such collection" (404),
     * and the duplicate-name case (422). The last two are both "no row came back", which is
     * why the duplicate is caught as an exception rather than inferred from an empty result
     * -- inferring it would collapse a 422 into a 404 and tell the user the collection they
     * are looking at does not exist.
     *
     * `updated_at` is moved by the trigger from 002_updated_at.sql, which this statement
     * does not mention and must not: that is the whole point of the trigger, and it is why
     * RETURNING is the only way to know what the row became.
     *
     * @return Collection|false|null The collection, false for a duplicate name, null for no
     *                               such row. A three-state return rather than exceptions
     *                               for the two failures, because both are ordinary answers
     *                               to an ordinary request and neither is exceptional in the
     *                               sense the service would catch.
     */
    public function rename(string $id, string $name): Collection|false|null
    {
        try {
            $rows = $this->db->connection()->selectFromWriteConnection(
                'UPDATE collections AS c SET name = ? WHERE c.id = ? RETURNING '.self::COLUMNS,
                [$name, $id],
            );
        } catch (QueryException $e) {
            if (! MemoRepository::isSqlState($e, self::UNIQUE_VIOLATION)) {
                throw $e;
            }

            return false;
        }

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Collection::fromRow($row) : null;
    }

    /**
     * Delete one collection, and say whether there was one.
     *
     * The memos it held are **not** deleted. `collection_id` is declared
     * `ON DELETE SET NULL` in 003_collections_and_reminders.sql, so they become fast memos
     * again and reappear in the strip -- which is what the user asking to delete a folder
     * means. That behaviour lives in the constraint rather than in a second UPDATE here, so
     * it also holds for a psql session and for anything else that ever deletes a row from
     * this table.
     *
     * @return bool Whether a row was deleted. False is a 404; a DELETE matching nothing is
     *              not an error in SQL, so this is the only way to tell.
     */
    public function delete(string $id): bool
    {
        return $this->db->connection()->delete('DELETE FROM collections WHERE id = ?', [$id]) > 0;
    }
}

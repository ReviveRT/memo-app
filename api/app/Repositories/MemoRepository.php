<?php

declare(strict_types=1);

namespace App\Repositories;

use App\Services\Memos\DeletedMemo;
use App\Services\Memos\Memo;
use App\Services\Memos\MemoAudio;
use App\Services\Memos\MemoQuery;
use App\Services\Owners\OwnerContext;
use Illuminate\Database\DatabaseManager;
use Illuminate\Database\QueryException;
use RuntimeException;
use stdClass;

/**
 * SQL for the memos table. Nothing above this namespace imports PDO or writes a
 * query, and no ORM is involved -- Eloquent is unused across this project per
 * MEMO-05, and the schema belongs to db/migrations rather than to PHP.
 *
 * Not final, unlike HealthRepository, and only for one reason: the feature suite
 * substitutes a fake for it. `php artisan test` runs against sqlite in memory (see
 * phpunit.xml) and every query below is Postgres-specific -- to_jsonb, to_char,
 * jsonb_agg, INSERT ... RETURNING over a table with a generated tsvector column -- so
 * the alternative to a seam here is a second, drifting definition of the schema in the
 * test suite. HealthEndpointTest gets to stay honest by misconfiguring the real
 * connection because "the database is unreachable" is a driver-independent
 * outcome; "the list came back newest first" is not. MEMO-25 owns the suite that
 * runs these statements against a real Postgres.
 */
class MemoRepository
{
    /**
     * The projection, shared by every statement below so the create response, the list
     * rows, the PATCH response and the reminder responses cannot drift apart.
     *
     * Enumerated rather than `*`, and that is a rule on this table rather than a
     * preference: `search_vector` is part of `*` and is the largest thing on the
     * row, so shipping it would put a full stemmed copy of every transcript into a
     * response nobody reads it from.
     *
     * Three conversions happen in SQL rather than in PHP:
     *
     *   * to_jsonb(tags), because the driver hands back Postgres' `{a,b}` array
     *     literal as an undifferentiated string and its quoting rules are not worth
     *     reimplementing. Memo::fromRow only has to json_decode.
     *   * to_char on created_at, so the wire format is one fixed, unambiguous
     *     string -- milliseconds, UTC, RFC 3339 -- instead of whatever the server's
     *     DateStyle and the session TimeZone would have produced for PHP to reparse.
     *     `AT TIME ZONE 'UTC'` is what makes the literal Z true.
     *   * the reminders aggregate, for both of those reasons at once. See below.
     *
     * That second one is aliased `created_at_iso` rather than `created_at`, and the
     * ugly name is load-bearing. ORDER BY resolves a bare name against the output
     * column labels before the table's columns, so with the obvious alias the
     * `ORDER BY created_at DESC` in list() below silently stops meaning the
     * timestamp column and starts meaning this formatted string. Caught here by
     * reading the plan rather than by reasoning: at 5,000 rows the aliased version
     * plans a Seq Scan plus a Sort on the to_char expression, while the same query
     * with the alias renamed is an Index Scan using memos_created_idx. It is not even
     * wrong -- this format sorts identically to the timestamp, so the rows come back
     * in the right order and only the index is quietly lost -- which is exactly why
     * it needs a name that cannot collide instead of a comment asking for care.
     * `ORDER BY memos.created_at DESC` also fixes it, and was rejected: it restores
     * the index scan by relying on the same subtlety, one dropped qualifier away from
     * regressing.
     *
     * **The reminders subquery.** A correlated scalar subquery producing one jsonb array,
     * ordered soonest-first off reminders_memo_idx. Three things about it are deliberate:
     *
     *   * `coalesce(..., '[]'::jsonb)`. jsonb_agg over no rows is NULL, not an empty
     *     array, so without this a memo with no reminders would carry `"reminders": null`
     *     and every reader would need a null check before iterating. Every memo now
     *     carries an array, empty or not.
     *   * The timestamps inside it go through the same to_char expression as
     *     created_at above, so a client parsing one format parses all of them.
     *     `to_char(NULL, ...)` is NULL, which is what an undelivered reminder should
     *     carry, so the pending case needs no special casing.
     *   * It survives `INSERT ... RETURNING`, which is what lets one constant serve both
     *     statements. Checked against a real Postgres rather than assumed, because a
     *     subquery referencing the inserted row is the kind of thing that either works
     *     or fails with a parse error: `RETURNING` resolves `memos.id` to the new row and
     *     answers `[]`, since a memo cannot have a reminder before it exists.
     *
     * The join it costs is paid on every list request, and it is bounded by the LIMIT
     * rather than by the size of the reminders table. It is here rather than fetched
     * separately because the fast strip badges a memo that has something pending, so
     * every row needs its reminders anyway -- a second request per memo is the thing
     * this avoids.
     */
    private const COLUMNS = <<<'SQL'
        id,
        source,
        status,
        transcript,
        title,
        summary,
        to_jsonb(tags) AS tags,

        -- Which of 'task', 'idea' or 'note' the enrichment pass filed this memo as.
        -- Projected ahead of the enricher so that landing MEMO-21 was a worker change
        -- alone, and written by it since.
        category,

        duration_ms,
        last_error,

        -- The sentence and the token for it always travel together, because a client
        -- that has one and not the other can either explain a failure or act on it but
        -- not both. 004_last_error_code.sql has why the token exists at all.
        last_error_code,

        -- What the worker was told to decode this in, or NULL for "detect it". The
        -- worker reads it from its own claim projection rather than this one, so what
        -- this line adds is the copy the browser gets: the stored choice, on the wire
        -- with the rest of the row. Nothing in the UI renders it today, and
        -- web/src/languages.js has why.
        language,
        collection_id,
        to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS created_at_iso,
        (
            SELECT coalesce(jsonb_agg(jsonb_build_object(
                'id', r.id,
                'remind_at', to_char(r.remind_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                'note', r.note,
                'delivered_at', to_char(r.delivered_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
            ) ORDER BY r.remind_at), '[]'::jsonb)
            FROM reminders r
            WHERE r.memo_id = memos.id
        ) AS reminders
        SQL;

    /**
     * SQLSTATE for foreign_key_violation.
     *
     * Reachable from one place: filing a memo into a collection id that does not exist.
     * That is a 404 rather than a 500 -- see moveToCollection, which is where the code is
     * turned into one.
     */
    private const FOREIGN_KEY_VIOLATION = '23503';

    public function __construct(
        private readonly DatabaseManager $db,
        private readonly OwnerContext $owner,
    ) {}

    /**
     * The owner every statement in this class is scoped by.
     *
     * A method rather than a property read at construction, because the container may build
     * this repository before ResolveOwner has run -- see OwnerContext, which throws if it is
     * asked for an owner that was never resolved. Calling it at query time is what makes
     * that throw impossible to reach through an HTTP request and guaranteed to fire outside
     * one.
     *
     * **Every statement below binds this in its WHERE, and none of them check ownership
     * afterwards in PHP.** That is deliberate and it is the reason no method here answers
     * 403. A memo belonging to somebody else matches no row, which is identical to a memo
     * that does not exist, which the controllers already turn into a 404. So a probe for
     * another owner's memo id cannot tell an id that is taken from one that is free -- the
     * scoping and the not-found path are the same code, and there is no second answer to
     * accidentally leak through.
     */
    private function ownerId(): string
    {
        return $this->owner->current()->id;
    }

    /**
     * One statement, and the row it just wrote comes back with it.
     *
     * RETURNING is what lets POST answer 201 with the full row without a follow-up
     * SELECT -- which is not merely a saved round trip: `created_at` is a column
     * default, so without RETURNING the response would either omit it or carry a
     * timestamp PHP invented that does not match the one in the table.
     *
     * The id is generated by the caller rather than by the database. 001_init.sql
     * declares `id uuid PRIMARY KEY` with no DEFAULT precisely so that omitting it
     * is a not-null violation instead of a silently unordered v4.
     *
     * The audio pair defaults to null rather than being required of every caller,
     * because "this memo has no recording" is what a text memo is -- the same reading
     * `transcript IS NULL` gets on the other side. Both columns are still named in the
     * statement and bound as NULL, so there is one INSERT rather than two shapes of
     * one.
     *
     * They travel together and are not independently optional: `audio_mime` describes
     * `audio_path`, and a row carrying one without the other would be either a blob
     * nothing can serve (MEMO-23 reads the mime to answer with) or a mime for a file
     * that does not exist. Nothing here enforces the pairing -- MemoService is the only
     * caller and passes both -- and it is not worth a CHECK constraint on a table
     * written by one statement in one file.
     *
     * `collection_id` is deliberately not a parameter. Every memo is created unfiled --
     * that is what a fast memo is -- and filing happens afterwards through
     * moveToCollection. Accepting it here would mean the recorder had to know which
     * collection was on screen, which is a coupling the strip exists to avoid.
     *
     * @param  string  $source  Memo::SOURCE_TEXT with a transcript and no audio, or
     *                          Memo::SOURCE_VOICE with audio and a null transcript.
     * @param  ?string  $audioPath  A storage *key*, relative to AUDIO_DIR -- never an
     *                              absolute path. See App\Contracts\AudioStorage.
     * @param  ?string  $language  A Whisper language code chosen by whoever recorded
     *                             this memo, or null to let the worker detect. Unlike
     *                             `collection_id` above this *is* a parameter, because
     *                             it is a property of the recording rather than of where
     *                             the memo is filed, and it has to be on the row before
     *                             a worker claims it -- which can happen a poll interval
     *                             after the INSERT commits.
     */
    public function insert(
        string $id,
        string $source,
        string $status,
        ?string $transcript,
        ?string $audioPath = null,
        ?string $audioMime = null,
        ?string $language = null,
    ): Memo {
        // selectFromWriteConnection, not select(). This is a write that returns
        // rows, and select() is the read path -- it is the same connection today
        // because config/database.php configures no read/write split, but the day
        // one is added a plain select() here would send an INSERT to a replica.
        $rows = $this->db->connection()->selectFromWriteConnection(
            'INSERT INTO memos (id, owner_id, source, status, transcript, audio_path, audio_mime, language)'
                .' VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING '.self::COLUMNS,
            [$id, $this->ownerId(), $source, $status, $transcript, $audioPath, $audioMime, $language],
        );

        $row = $rows[0] ?? null;

        if (! $row instanceof stdClass) {
            // Unreachable via Postgres: an INSERT ... RETURNING that inserts a row
            // returns it, and anything that stopped the insert -- the CHECK
            // constraints on source and status, a duplicate id -- raises instead.
            // Asserted rather than assumed because the alternative is a TypeError
            // from Memo::fromRow, which reads as a mapping bug rather than as a
            // write that did not happen.
            throw new RuntimeException("INSERT of memo {$id} returned no row.");
        }

        return Memo::fromRow($row);
    }

    /**
     * The list, newest first, narrowed by whichever of MemoQuery's filters are set.
     *
     * One statement assembled from optional predicates, replacing the `recent()` and
     * `search()` pair this class used to have. That pair did not survive the filters
     * growing to four independent dimensions -- text, a from bound, a to bound and a
     * collection scope -- because eight combinations is eight methods, and the point of
     * MemoQuery is that the call site names what it wants instead of picking one.
     *
     * With no filters at all the assembled statement is now
     * `SELECT ... FROM memos WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?`, which
     * memos_owner_created_idx serves as an Index Scan in output order -- the same shape the
     * unfiltered list had before owners existed, one equality deeper.
     *
     * That predicate is not optional and has no filter behind it. It is seeded before the
     * assembly below rather than added to it; 007_owners.sql has why every read in this
     * application is scoped and what it would mean if one were not.
     *
     * **The text predicate is unchanged, and its three arms still are what they were.**
     *
     * 1. `websearch_to_tsquery`, not `to_tsquery` or `plainto_tsquery`. It gives quoted
     *    phrases and minus-exclusion for free -- `"call the dentist"` compiles to
     *    `'call' <2> 'dentist'` and `dentist -thursday` to `'dentist' & !'thursday'` --
     *    and, unlike to_tsquery, it never raises on input a human typed. That is the part
     *    that matters, because the input is a search box: `to_tsquery('english', 'dentist
     *    &')` is `ERROR: no operand in tsquery` and `'a | | b'` is `ERROR: syntax error in
     *    tsquery`, both of which would be a 500 for a half-typed query.
     *    websearch_to_tsquery answers `'dentist'` and `'b'`.
     *
     * 2. The ILIKE fallback, for the two cases the tsvector cannot reach: a partial word,
     *    and a run-together token. Both are spelled out in 001_init.sql next to the index
     *    that serves them. Not the similarity operator -- see likePattern below and the
     *    measurements in 001_init.sql for why `%` silently matches nothing here.
     *
     * 3. The in-flight pin. A memo still being transcribed has no transcript, so it
     *    matches nothing and would vanish from the list the moment a filter was active --
     *    right after the user recorded it. Memo::inFlightStatuses() says which statuses
     *    that covers and why 'failed' is not one of them.
     *
     * **Where the pin sits is the one thing this method changed about search, and it is a
     * correctness fix rather than a refactor.** The pin is inside the text group's
     * parentheses, so it is OR'd with the two text arms and then AND'd with the window and
     * the collection scope. Written the other way -- as a fourth top-level OR -- it would
     * escape both, and both escapes are wrong in a way that reads as a bug in the filter:
     *
     *   * A memo recorded ten seconds ago would appear in a list filtered to *yesterday*,
     *     because it is queued. The date filter would look broken, and the user cannot
     *     tell that the row is there on purpose.
     *   * That same memo, which is unfiled, would appear inside every collection's list.
     *     A collection would appear to contain a memo that was never put in it.
     *
     * So the pin is scoped: it keeps an in-flight memo visible in the list it *belongs*
     * to while it is still being worked on, and nowhere else. The window and the scope are
     * hard bounds with no exception, which is what a filter naming a date range or a
     * folder has to be.
     *
     * **Ordering: created_at DESC, whatever the filters.** Both 001_init.sql and COLUMNS
     * above expected a ts_rank ordering. Three measurements against the schema said
     * otherwise, and the third is decisive:
     *
     *   * ts_rank cannot rank the ILIKE half of the predicate. A row matched only by
     *     substring does not match the tsquery, so its rank is exactly 0 -- the same trap
     *     001_init.sql documented for tag-only rows under ts_rank_cd, one arm further
     *     along. Ordering by rank would bury every partial-word hit beneath every
     *     full-text hit.
     *   * where ts_rank does apply, it barely separates anything: 0.0760 for a transcript
     *     match against 0.0608 for a tag-only match on the same query. It encodes which
     *     column matched more than how relevant the memo is.
     *   * rank ordering puts the pinned in-flight rows *last*. They score 0 like every
     *     other non-tsquery match, so `ORDER BY rank DESC` sorts them below every hit --
     *     measured, both of them under both dentist memos -- and with a LIMIT they are
     *     dropped outright once there are `limit` matches. The memo the pin exists to
     *     keep on screen is the first thing rank ordering throws away.
     *
     * created_at DESC needs no special term for any of that, and it is now also the only
     * ordering that makes the date filter legible: a list filtered to a range should read
     * in the same direction as the unfiltered one.
     *
     * **The plans below were measured on 5,002 rows before this table had owners, and are
     * left here as history rather than as current fact.** Every one of them was taken
     * against a query with no leading equality, and 007_owners.sql added one to all four
     * shapes plus the two indexes they name have been replaced by owner-first composites.
     * Which plan each reading now gets has not been re-measured; restating these numbers as
     * though it had would be worse than leaving the question open. What they still document
     * is the *reasoning* -- which is why they are kept:
     *
     *   * `collection = <uuid>` was a Bitmap Index Scan on memos_collection_idx (now
     *     memos_owner_collection_idx) with the text arms as a recheck filter.
     *   * a date window was an Index Cond on memos_created_idx (now
     *     memos_owner_created_idx) -- `created_at >= ... AND created_at < ...` -- with
     *     everything else filtered on top. That is the plan a bounded list wants: the index
     *     walk is already in output order and the window ends it.
     *   * `collection = none` with four fifths of the table unfiled did *not* use
     *     memos_collection_idx, and should not have: it walked memos_created_idx and
     *     filtered, which the LIMIT makes cheaper because nearly every row is a match. 003's
     *     comment on that index has the inverted measurement. Note that the premise --
     *     "four fifths of the table" -- is itself now per-owner, so this is the reading most
     *     likely to have changed.
     *
     * @return list<Memo>
     */
    public function list(MemoQuery $query): array
    {
        // The owner leads, always, and is the one predicate no caller can omit. It is
        // seeded here rather than appended with the optional filters so that it cannot be
        // reordered behind them by a later edit -- memos_owner_created_idx wants the
        // equality first, and more importantly a reader checking that this list is scoped
        // should find the answer on the first line of the method rather than by reading to
        // the end of the assembly.
        /** @var list<string> $where */
        $where = ['owner_id = ?'];

        /** @var list<mixed> $bindings */
        $bindings = [$this->ownerId()];

        if ($query->text !== null) {
            // Built from the constant rather than written out, so adding a status to
            // Memo::inFlightStatuses() cannot leave the placeholder count behind and turn
            // a lifecycle change into a bound-parameter mismatch.
            $inFlight = implode(', ', array_fill(0, count(Memo::inFlightStatuses()), '?'));

            $where[] = "(search_vector @@ websearch_to_tsquery('english', ?)"
                .' OR transcript ILIKE ?'
                ." OR status IN ({$inFlight}))";

            $bindings[] = $query->text;
            $bindings[] = self::likePattern($query->text);

            foreach (Memo::inFlightStatuses() as $status) {
                $bindings[] = $status;
            }
        }

        // Two separate predicates rather than a BETWEEN, because BETWEEN is inclusive at
        // both ends and this interval is half-open. See App\Support\TimeWindow for why the
        // last millisecond of a day is not something to hand-wave.
        if ($query->window->from !== null) {
            $where[] = 'created_at >= ?';
            $bindings[] = $query->window->from;
        }

        if ($query->window->to !== null) {
            $where[] = 'created_at < ?';
            $bindings[] = $query->window->to;
        }

        // `unfiledOnly` wins over `collectionId` if both were somehow set, and nothing can
        // set both: ListMemosRequest spells the scope as one `?collection=` parameter, so
        // there is no request that asks for a memo that is both filed and unfiled. The
        // elseif states the precedence anyway rather than leaving it to argument order.
        if ($query->unfiledOnly) {
            $where[] = 'collection_id IS NULL';
        } elseif ($query->collectionId !== null) {
            $where[] = 'collection_id = ?';
            $bindings[] = $query->collectionId;
        }

        // $limit is bound rather than interpolated. Laravel binds a PHP int as
        // PDO::PARAM_INT, which is what a parameterised LIMIT needs; the value reaching
        // here has already been validated by ListMemosRequest, so the bound parameter
        // is defence in depth rather than the only check.
        $bindings[] = $query->limit;

        // No `$where === []` branch any more: the owner predicate above guarantees at least
        // one, so the WHERE clause is unconditional and the empty case it used to guard
        // against is unreachable.
        $rows = $this->db->connection()->select(
            'SELECT '.self::COLUMNS
                ."\nFROM memos"
                ."\nWHERE ".implode("\n  AND ", $where)
                ."\nORDER BY created_at DESC"
                ."\nLIMIT ?",
            $bindings,
        );

        return $this->hydrate($rows);
    }

    /**
     * One memo by id, or null when there is no such row.
     *
     * Exists for the reminder routes: adding or acknowledging a reminder answers with the
     * memo it belongs to rather than with the reminder alone, so the frontend reconciles
     * one shape by id and never has to merge a reminder into a row itself. The read after
     * the write is what makes that possible without duplicating the aggregate in COLUMNS
     * into a second projection.
     */
    public function find(string $id): ?Memo
    {
        $rows = $this->db->connection()->select(
            'SELECT '.self::COLUMNS.' FROM memos WHERE id = ? AND owner_id = ?',
            [$id, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Memo::fromRow($row) : null;
    }

    /**
     * Where one memo's recording is kept and what it is, or null when it has none (MEMO-23).
     *
     * **Two columns rather than COLUMNS, which is the whole reason this is not `find()` plus a
     * getter.** The playback route needs a storage key and a media type; the projection every
     * other route answers with carries the transcript, the summary, the tags and a correlated
     * subquery over the reminders table. A browser seeking through a recording issues one
     * request per drag, and each of those would otherwise read a memo's entire text to find out
     * which file to open. The reminders subquery alone is a join paid for nothing here.
     *
     * It is also the projection that keeps `audio_path` off the wire. COLUMNS is what responses
     * are built from and this is not in it -- see DeletedMemo and MemoAudio for the argument
     * that a storage key is not a client's to hold, and AudioStorage for what a caller holding
     * one may do with it.
     *
     * Null covers three cases the caller answers identically: no such memo, a typed memo, and
     * -- reachable only through hand-written SQL -- a row with `audio_mime` set and no path.
     * They are not told apart on purpose. The first is a 404 by any reading; the second is the
     * ordinary state of every text memo and not an error anywhere below HTTP; and all three
     * leave the caller with nothing to serve, which is one answer and not three.
     *
     * An empty-string path is folded into null rather than passed on. The column is nullable
     * and nothing writes `''`, so this is defending against a hand-written row -- but the cost
     * of not doing it is a key that AudioStorage refuses with a StorageException, which is a
     * 500 for a memo that simply has no recording.
     *
     * **This is the one owner check in the application that no amount of frontend care could
     * have supplied.** The request that reaches here is issued by an `<audio>` element's
     * `src` (MemoDialog.vue), not by the fetch wrapper -- the browser makes it, so no code in
     * web/src can attach anything to it. An identity carried in a header or in localStorage
     * would therefore have left recordings readable by memo id alone, and would have done so
     * silently, because every JSON route would have looked correctly scoped. The identity is
     * a cookie precisely so that the browser attaches it to a request the application never
     * sees. See 007_owners.sql.
     */
    public function audioFor(string $id): ?MemoAudio
    {
        $rows = $this->db->connection()->select(
            'SELECT audio_path, audio_mime FROM memos WHERE id = ? AND owner_id = ?',
            [$id, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        if (! $row instanceof stdClass) {
            return null;
        }

        $key = $row->audio_path ?? null;

        if (! is_string($key) || $key === '') {
            return null;
        }

        $mimeType = $row->audio_mime ?? null;

        return new MemoAudio(
            key: $key,
            mimeType: is_string($mimeType) && $mimeType !== '' ? $mimeType : null,
        );
    }

    /**
     * File a memo into a collection, or move it back to the fast strip with null.
     *
     * One UPDATE with RETURNING, so the caller gets the memo in its new state without a
     * follow-up SELECT -- the same reason insert() returns a row, and it matters more here
     * because `updated_at` is set by the trigger from 002 and PHP has no way to know what
     * it became.
     *
     * Returns null for "no such memo", which the controller turns into a 404. An UPDATE
     * that matches nothing is not an error in SQL, so distinguishing it from a successful
     * write is exactly what checking the returned row is for.
     *
     * **Both failure modes are the caller's fault and both are 404s, not 500s.** A memo id
     * that does not exist matches no row and comes back null. A *collection* id that does
     * not exist raises a foreign key violation, which is caught here and returned as
     * false-ish in the same way -- because from the client's side they are the same
     * mistake, naming something that is not there, and a 500 for one of them would put a
     * stack trace on stderr for a bad request. The controller has no way to tell the two
     * apart afterwards and does not need to; the message it answers with names both
     * possibilities.
     *
     * **There is a third failure mode and it takes the same path: a collection that exists
     * and belongs to somebody else.** Nothing in this statement checks for it, deliberately.
     * 007_owners.sql makes `memos.collection_id` a composite foreign key over
     * `(collection_id, owner_id)`, so a stranger's collection fails the constraint exactly as
     * an absent one does, raises the same 23503, and is caught below into the same null. The
     * check lives in the schema rather than here because a predicate in this method would
     * hold only for as long as this stays the only statement writing the column, and because
     * "no such collection" and "not your collection" are the same mistake from the client's
     * side and deserve one answer.
     *
     * Not wrapped in a transaction: it is one statement.
     *
     * @param  ?string  $collectionId  Null unfiles the memo. That is a real operation
     *                                 rather than a cleared field -- it is how a memo gets
     *                                 back out of a collection the user put it in by
     *                                 mistake -- so it is spelled as a value and not as an
     *                                 omitted argument.
     */
    public function moveToCollection(string $memoId, ?string $collectionId): ?Memo
    {
        try {
            $rows = $this->db->connection()->selectFromWriteConnection(
                'UPDATE memos SET collection_id = ? WHERE id = ? AND owner_id = ? RETURNING '.self::COLUMNS,
                [$collectionId, $memoId, $this->ownerId()],
            );
        } catch (QueryException $e) {
            // Rethrown unless it is the one code that means "you named a collection that
            // does not exist". Anything else here -- a dead connection, a disk full -- is
            // genuinely a 500 and must not be flattened into a 404.
            if (! self::isSqlState($e, self::FOREIGN_KEY_VIOLATION)) {
                throw $e;
            }

            return null;
        }

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Memo::fromRow($row) : null;
    }

    /**
     * Rename a memo.
     *
     * One of the two columns of a memo's *content* a client may write -- `transcript` is the
     * other, see correctTranscript above. `title` is filled by the enrichment pass from the
     * transcript, so it is a *guess* about what a memo is called: a good one, often, and wrong
     * often enough that a memo the owner cannot rename is a memo they cannot find later.
     *
     * This block used to add "that is the opposite of the transcript, which is a record of what
     * was said and is nobody's to edit". That is no longer true and the argument did not
     * survive a wrong transcript -- UpdateMemoRequest has why, from the validation side.
     *
     * The same shape as moveToCollection -- one UPDATE, RETURNING the whole row -- and for
     * the same reason: the trigger from 002 moves `updated_at`, so a caller that wanted the
     * memo in its new state would otherwise need a second SELECT that could race the first.
     *
     * No foreign key to violate here, so there is no QueryException to classify: a null row
     * back means the memo does not exist, and that is the only failure this can have.
     *
     * @param  ?string  $title  Null clears it, which is a real operation: a memo whose title
     *                          the owner has cleared falls back to the first line of its own
     *                          transcript everywhere it is rendered, which is a better label
     *                          than an enrichment guess they disagreed with.
     */
    /**
     * Replace a memo's transcript with a corrected one.
     *
     * Its own statement rather than a second SET on `rename`, matching how `moveToCollection`
     * and `rename` already sit beside each other: they touch different columns, answer the
     * same shape, and the controller runs whichever the body asked for. A combined statement
     * would need every caller to say which halves it meant.
     *
     * **`search_vector` recomputes itself, but only for the columns this statement writes.**
     * It is a STORED generated column over title, summary, transcript and tags
     * (001_init.sql), so Postgres rebuilds it as part of this UPDATE and a corrected
     * transcript is findable by its new words without a line of code. That is the whole
     * reason the column is generated rather than maintained by a trigger.
     *
     * **The other half of that sentence needed MEMO-21 to stay true.** This block used to
     * end "and no longer findable by the wrong ones", which held while `summary` and `tags`
     * were always NULL. They are not any more: the enrichment pass fills both from the
     * transcript, both are in the same generated column, and a correction that left them
     * would keep the *old* words findable through the summary while the transcript no longer
     * contains them. Searching for a word somebody had just corrected away would still
     * return the memo, which is precisely the bug the generated column is supposed to make
     * impossible.
     *
     * So the derived columns are cleared here, and clearing them costs nobody anything they
     * typed: `summary`, `tags` and `category` are written only by the worker, and
     * `UpdateMemoRequest` accepts only `collection_id`, `title` and `transcript` -- there is
     * no user edit to lose. `enriched_at` and `enrichment_error` go with them, because both
     * describe an enrichment of text that is no longer on the row.
     *
     * What this deliberately does *not* do is re-enrich. That would mean putting a `ready`
     * memo back to `queued`, which re-runs transcription's guards for a memo whose audio has
     * not changed and reopens the title-overwrite hazard NOTES.md records against requeueing.
     * A memo with a corrected transcript and no summary is honest; a fresh summary for it is
     * a feature, not a fix.
     *
     * The title is deliberately *not* touched, and it is the one exception to the paragraph
     * above. It may have been cut from the text being replaced, so it can now be stale -- but
     * it may equally be one the owner typed, and this route has a `title` field of its own
     * for changing it. Rewriting it as a side effect of a different edit is the kind of
     * helpfulness that loses somebody's work; the UI puts the two fields next to each other
     * so a stale title is visible while it is being corrected.
     *
     * That exception has a search consequence worth stating rather than leaving to be found.
     * Verified against a real Postgres on a memo whose transcript, summary, tags and title
     * all carried the same wrong name: after this statement the summary and tag words are no
     * longer matched, the corrected words are, and the *title's* copy of the old name still
     * is. That is the intended trade -- one visible, editable field keeps one stale word
     * findable, rather than the invisible ones keeping a whole sentence of them.
     */
    public function correctTranscript(string $memoId, string $transcript): ?Memo
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            <<<'SQL'
                UPDATE memos
                   SET transcript = ?,
                       summary = NULL,
                       tags = '{}',
                       category = NULL,
                       enriched_at = NULL,
                       enrichment_error = NULL
                 WHERE id = ?
                   AND owner_id = ?
                RETURNING
                SQL.' '.self::COLUMNS,
            [$transcript, $memoId, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Memo::fromRow($row) : null;
    }

    public function rename(string $memoId, ?string $title): ?Memo
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            'UPDATE memos SET title = ? WHERE id = ? AND owner_id = ? RETURNING '.self::COLUMNS,
            [$title, $memoId, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Memo::fromRow($row) : null;
    }

    /**
     * Hand a failed memo back to the worker's queue, or answer null if it was not failed.
     *
     * **`AND status = 'failed'` is the whole safety of this statement, not a tidy way to
     * answer 404.** Without it the route would requeue a memo in any state, and two of the
     * four are actively dangerous:
     *
     *   * `processing` -- a claim is live, and its owner's writes are fenced on `locked_at`
     *     (memo_ai/memos.py). Setting `status='queued'` while that token is still on the row
     *     makes it claimable again, so a second replica picks up a memo the first is midway
     *     through and both transcribe it. The fence protects the *writes*; nothing protects
     *     the claim predicate from a third party moving the status underneath it.
     *   * `ready` -- a finished memo would be re-enriched for nothing, and its `title` (which
     *     the owner may have edited by hand -- see rename above) overwritten by a fresh guess.
     *
     * So the predicate is the check, done by the same statement that does the work rather
     * than by a SELECT in front of it -- which would be a race as well as a round trip. The
     * caller distinguishes "no such memo" from "not failed" with a second read; see
     * MemoService::retry for why that read is allowed to be racy and this one is not.
     *
     * `attempts = 0` is what makes this a retry rather than a gesture. A failed memo sits at
     * the cap, and the worker's claim increments before any of its code runs (`_CLAIM`), so a
     * requeued row at `attempts = MAX_ATTEMPTS` gets exactly one more go and `fail_or_retry`
     * reads it as already exhausted -- no backoff, no second attempt, and the reaper's
     * `attempts < max_attempts` requeue never matches it either. Zero gives it the same
     * budget a new memo has, which is the honest meaning of a person pressing Retry.
     *
     * `next_attempt_at = now()` for the same reason from the other side: the column may hold
     * a backoff from the attempt that failed, and the claim predicate is
     * `next_attempt_at <= now()`. A press that produced no visible change for thirty seconds
     * would read as a button that does not work.
     *
     * `locked_at = NULL` is already true of every row `failed` is reachable from -- both
     * `_FAIL` and the reaper clear it -- and it is set anyway, because it costs nothing and
     * the one row it protects against is the one nothing else can: a `failed` row with a
     * stale token, written by hand or by a future writer, would go back to the queue holding
     * a fence somebody else could still match.
     *
     * `last_error` is deliberately left where it is. It is the *last* error, not the current
     * state -- the worker's own retry path writes it onto a `queued` row for exactly that
     * reason -- and the frontend gates the reason on `status === 'failed'`, so a requeued
     * memo stops showing it without the column being touched. The next successful
     * transcription clears it (`_COMMIT_TRANSCRIPT`), which is the write that knows the
     * error is over.
     */
    public function requeue(string $memoId): ?Memo
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            <<<'SQL'
                UPDATE memos
                   SET status = 'queued',
                       attempts = 0,
                       next_attempt_at = now(),
                       locked_at = NULL
                 WHERE id = ?
                   AND owner_id = ?
                   AND status = 'failed'
                RETURNING
                SQL.' '.self::COLUMNS,
            [$memoId, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        return $row instanceof stdClass ? Memo::fromRow($row) : null;
    }

    /**
     * Delete a memo, and answer with what it was.
     *
     * **RETURNING on a DELETE, which is the whole reason this returns a Memo rather than a
     * bool.** The caller has to unlink the audio blob afterwards, and `audio_path` only
     * exists on the row being deleted -- so a `DELETE` that answered "1 row" would force a
     * SELECT first, and a SELECT-then-DELETE is a race: two clients deleting the same memo
     * would both read the path and both try to unlink it, and one of them would report a
     * failure for work the other had already done. One statement makes the row's contents
     * and its removal the same atomic fact, and exactly one caller can win it.
     *
     * `ON DELETE CASCADE` on `reminders.memo_id` takes the reminders with it, inside
     * Postgres, where no response can observe it. That is stated in the constraint rather
     * than performed here for the reason 003 gives about `ON DELETE SET NULL` on
     * `collection_id`: it then holds for every deleter of this table and not only for this
     * route.
     *
     * Null when there is no such memo, which the controller turns into a 404 -- the same
     * reading moveToCollection gives an UPDATE that matched nothing.
     *
     * `audio_path` is appended to the projection rather than added to COLUMNS, because it is
     * a storage key and COLUMNS is what every response is built from. DeletedMemo has the
     * argument for keeping it off the wire.
     */
    public function delete(string $memoId): ?DeletedMemo
    {
        $rows = $this->db->connection()->selectFromWriteConnection(
            'DELETE FROM memos WHERE id = ? AND owner_id = ? RETURNING '.self::COLUMNS.', audio_path',
            [$memoId, $this->ownerId()],
        );

        $row = $rows[0] ?? null;

        if (! $row instanceof stdClass) {
            return null;
        }

        $path = $row->audio_path ?? null;

        return new DeletedMemo(
            memo: Memo::fromRow($row),
            audioPath: is_string($path) && $path !== '' ? $path : null,
        );
    }

    /**
     * Wraps a user's query in `%` for a substring ILIKE, escaping the three characters
     * that would otherwise be pattern syntax.
     *
     * This is not hygiene -- the query is a bound parameter and was never an injection
     * risk. It is that `%` and `_` are wildcards inside a LIKE pattern, so without this
     * the user's own punctuation quietly changes what they searched for. Measured against
     * the fixture: unescaped, `q=50%` builds the pattern `%50%%`, whose trailing pair
     * reads as "anything", and it matched **102 rows** where one memo mentions a margin of
     * 50%. `_` is the quieter version of the same bug -- it matches exactly one character,
     * so `created_at` and `createdXat` are the same query.
     *
     * Backslash is escaped first and for a different reason: it is LIKE's default escape
     * character, so a lone trailing `\` in the query would leave a dangling escape and
     * make Postgres raise on the pattern. Order matters -- doing `%` and `_` first would
     * then escape the backslashes this step just added.
     *
     * The escaped pattern is still index-driven; `%created\_%` plans as a Bitmap Index
     * Scan on memos_trgm_idx exactly as the unescaped one does, so this costs nothing but
     * correctness. What it cannot fix is that a pattern with fewer than three
     * non-wildcard characters has no trigram to look up and falls back to a Seq Scan --
     * `%re%` scans all 5,006 rows. That is a property of trigram indexes rather than of
     * this function, and it is bounded by MAX_LIMIT and the size of this table.
     *
     * Public and static so it can be tested for what it returns rather than for what
     * Postgres does with it: LikePatternTest asserts the pattern this builds for `50%`, so
     * what is pinned is the escaping that prevents those 102 rows rather than the count
     * itself, which would need a live database to reach. CollectionRepository reuses it
     * for the same reason on its own name filter.
     */
    public static function likePattern(string $query): string
    {
        return '%'.str_replace(['\\', '%', '_'], ['\\\\', '\\%', '\\_'], $query).'%';
    }

    /**
     * Whether a QueryException carries a particular SQLSTATE.
     *
     * `errorInfo[0]` first, and `getCode()` only as a fallback, because the two do not
     * agree as reliably as they look. PDOException::getCode() is documented as the
     * SQLSTATE and is a *string* there rather than the int the Throwable interface
     * declares -- but a QueryException raised before the driver ever answered (a
     * connection that could not be opened) carries no errorInfo and a code of 0, and
     * some drivers report `HY000` in getCode() while putting the real state in
     * errorInfo. Reading the array first and comparing as a string is what makes this
     * mean the same thing in every case.
     *
     * Public and static so CollectionRepository can use it for the unique-name violation
     * without a second copy of the same subtlety.
     */
    public static function isSqlState(QueryException $e, string $sqlState): bool
    {
        $reported = $e->errorInfo[0] ?? $e->getCode();

        return is_scalar($reported) && (string) $reported === $sqlState;
    }

    /**
     * @param  list<stdClass>  $rows
     * @return list<Memo>
     */
    private function hydrate(array $rows): array
    {
        return array_values(array_map(
            static fn (stdClass $row): Memo => Memo::fromRow($row),
            $rows,
        ));
    }
}

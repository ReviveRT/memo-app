-- 003_collections_and_reminders.sql — the two things a memo can now have besides a
-- transcript: a collection it belongs to, and reminders that fire about it.
--
-- Same two rules as every file in this directory, because db/migrate.sh wraps it and
-- its ledger row in one `psql --single-transaction` call:
--
--   * no BEGIN/COMMIT in here
--   * nothing illegal inside a transaction block: CREATE INDEX CONCURRENTLY,
--     CREATE DATABASE, VACUUM
--
-- Two tables and one column, and the shape of each was decided by what deleting
-- things has to mean. That is spelled out at each foreign key rather than here,
-- because it is the kind of thing a reader checks at the constraint.

-- --------------------------------------------------------------------------
-- Collections
-- --------------------------------------------------------------------------

CREATE TABLE collections (
    -- No DEFAULT, for the reason 001_init.sql gives about memos.id and it applies
    -- unchanged: the API mints a UUIDv7 itself so the 201 can carry the id without a
    -- second round trip, and v7 sorts by time. Do not "complete" this with
    -- DEFAULT gen_random_uuid() — that emits v4, whose ordering is arbitrary, and it
    -- would apply silently to any INSERT omitting the column, turning a missing id in
    -- the write path into unordered rows rather than the not-null violation that
    -- would have caught it.
    id         uuid PRIMARY KEY,

    -- The user's own words: "Memos for Work". Not derived from anything and never
    -- generated, which makes it the one column in this schema whose content is typed
    -- by a human and read back verbatim.
    --
    -- The CHECK is on the trimmed value, not on the raw one. `name text NOT NULL`
    -- alone admits '' and '   ', and a collection with a blank name is not a bad
    -- record so much as an unreachable one: it renders as an empty card, matches no
    -- search, and can only be got rid of by whoever can find it. The API trims and
    -- rejects blanks too (StoreCollectionRequest); this is the half that also holds
    -- for a hand-rolled psql session.
    --
    -- No length cap here, matching the rest of this schema — `transcript` has none
    -- either. The cap belongs where the 422 is worded.
    name       text NOT NULL CHECK (btrim(name) <> ''),

    created_at timestamptz NOT NULL DEFAULT now(),

    -- Maintained by the trigger below, not by the default. See 002_updated_at.sql for
    -- why this table needs one at all: Postgres has no ON UPDATE CURRENT_TIMESTAMP,
    -- so the default covers the INSERT and nothing else.
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Reuses set_updated_at() from 002_updated_at.sql rather than declaring a second
-- function that does the same thing. The function is schema-wide and takes no
-- arguments; only the trigger is per-table.
--
-- CREATE OR REPLACE TRIGGER, not a bare CREATE TRIGGER, for the reason 002 gives:
-- this file must stay replayable against a database that already has the trigger.
-- Triggers have no IF NOT EXISTS form.
--
-- Unlike memos, nothing here would be caught by the OLD.* IS DISTINCT FROM NEW.*
-- guard that 002 rejected — this table has no generated column to make that
-- comparison lie. It is still not added, because a no-op UPDATE on a two-column
-- table is not a case worth optimising for and one table behaving differently from
-- the other would be a difference with no reason behind it.
CREATE OR REPLACE TRIGGER collections_set_updated_at
    BEFORE UPDATE ON collections
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- Two collections called "Work" are not a schema error but they are a usability
-- one: the cards are indistinguishable, and a memo filed into the wrong one is not
-- recoverable by looking. Unique on the folded, trimmed name so "work", "Work" and
-- " Work " are the same collection.
--
-- An expression index rather than a UNIQUE constraint, because a constraint cannot be
-- declared over an expression. The consequence for the API is that a duplicate raises
-- SQLSTATE 23505 (unique_violation) with this index's name in it rather than a named
-- constraint's — CollectionRepository catches that code and answers 422, and the name
-- below is what it would appear as in a log.
--
-- lower() rather than a collation-aware fold: it is IMMUTABLE, which an index
-- expression must be, and this is a local single-user app rather than one indexing
-- Turkish dotless i. btrim() matches the CHECK above, so a name that passes the check
-- is the same string this index sees.
CREATE UNIQUE INDEX collections_name_key ON collections (lower(btrim(name)));

-- The collections list's only ordering: newest first, the same as memos.
CREATE INDEX collections_created_idx ON collections (created_at DESC);

-- --------------------------------------------------------------------------
-- A memo's collection
-- --------------------------------------------------------------------------

-- Nullable, and NULL is a first-class value here rather than missing data: it is
-- exactly what the UI calls a "fast memo" — one recorded without stopping to file it.
-- So the fast strip is `WHERE collection_id IS NULL` and needs no second column, no
-- boolean, and no sentinel "Unsorted" row that could be renamed or deleted.
--
-- One collection per memo, not many. A memo is filed rather than labelled: the whole
-- point of the strip is that "unorganized" has one unambiguous meaning, and with a
-- join table that becomes "in zero collections" — a NOT EXISTS whose answer changes
-- when a memo is added to a second collection. If labels are ever wanted, this column
-- becomes the primary one and a join table is added beside it; nothing here has to be
-- undone.
--
-- ON DELETE SET NULL, which is the important half of this line. CASCADE would delete
-- the memos along with the collection — the transcript, the recording's row, all of it
-- — so deleting a folder would destroy its contents, and the user asking to delete
-- "Memos for Work" is asking about the folder. SET NULL returns them to the fast strip,
-- where they are visible and can be filed again. RESTRICT was the other candidate and
-- is worse in a different way: it makes a collection undeletable while it holds
-- anything, which means emptying it by hand first.
ALTER TABLE memos ADD COLUMN collection_id uuid REFERENCES collections(id) ON DELETE SET NULL;

-- Serves both readings of the column: one collection's memos newest first, and the
-- fast strip's `collection_id IS NULL` newest first. One index rather than a second
-- partial one for the NULL case, because a btree does index NULLs — `IS NULL` becomes an
-- Index Cond rather than a filter.
--
-- Which plan each reading actually gets was measured on 5,002 rows, and it is worth
-- writing down because only one of the two ever uses this index — and that is the
-- correct outcome rather than a shortfall:
--
--   * `collection_id = $1` is an Index Cond here. A Bitmap Index Scan when a text
--     filter is OR'd on top of it, a plain Index Scan when it is not.
--   * `collection_id IS NULL`, with four fifths of the table unfiled, does **not** use
--     it. It walks memos_created_idx with `Filter: (collection_id IS NULL)`, which the
--     LIMIT makes the better plan: when most memos are unfiled, the newest 50 rows are
--     nearly all matches, so the scan stops almost immediately and there is no sort.
--     Inverting the fixture — 3 unfiled rows out of 5,002 — flips it to `Bitmap Index
--     Scan on memos_collection_idx, Index Cond: (collection_id IS NULL)`.
--
-- So this index carries the collection reading always, and the fast-strip reading
-- exactly when the strip is short — which is the case where scanning created_at would
-- otherwise run the length of the table to turn up a handful of rows.
CREATE INDEX memos_collection_idx ON memos (collection_id, created_at DESC);

-- --------------------------------------------------------------------------
-- Reminders
-- --------------------------------------------------------------------------

-- A separate table rather than remind_at/remind_note columns on the memo, because the
-- UI offers two controls — an alarm at a wall-clock time, and a timer some minutes out
-- — and they are not alternatives. Setting one must not silently clear the other,
-- which is what a single column pair would do.
--
-- What that costs is a join on the memo projection, since the list badges a memo that
-- has something pending. MemoRepository::COLUMNS pays it as one correlated subquery
-- returning jsonb.
CREATE TABLE reminders (
    id         uuid PRIMARY KEY,

    -- ON DELETE CASCADE, the opposite of collection_id above, and for a reason rather
    -- than for variety: a reminder is *about* a memo and has no meaning without it,
    -- while a memo has plenty of meaning without a collection. There is no delete path
    -- for a memo yet (nothing owns one before MEMO-17), so this constraint currently
    -- fires for nothing — it is here so that whoever adds one does not have to discover
    -- that reminders outlive their memos.
    memo_id    uuid NOT NULL REFERENCES memos(id) ON DELETE CASCADE,

    -- When to notify. An absolute instant, always: a relative "in 30 minutes" is
    -- resolved against the browser's clock before it is sent, so nothing here has to
    -- know what "in" meant or when it was said.
    remind_at  timestamptz NOT NULL,

    -- "about something" — the user's note for why they wanted reminding. Optional,
    -- because the memo it hangs off is usually the answer.
    note       text,

    -- When it was actually shown, or NULL for "still owed". This is what makes a
    -- reminder fire once rather than once per page load, and it is stored rather than
    -- kept in the browser because the browser is not where the reminder lives.
    --
    -- Deliberately not a boolean. A reminder shown four hours late and one shown on
    -- time are both "delivered", and the difference is the whole of what a user
    -- complains about; a timestamp can answer it and a flag cannot.
    delivered_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now()
);

-- The projection's join: every reminder for one memo. Includes remind_at so the
-- soonest-first ordering inside the aggregate comes off the index rather than out of a
-- sort.
CREATE INDEX reminders_memo_idx ON reminders (memo_id, remind_at);

-- "What is due and has not been shown", which is the only question asked of this table
-- across all memos. Partial on the undelivered rows: delivered reminders are the ones
-- that accumulate, and none of them can ever match.
CREATE INDEX reminders_due_idx ON reminders (remind_at) WHERE delivered_at IS NULL;

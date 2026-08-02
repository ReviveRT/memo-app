-- 007_owners.sql — whose memo is it.
--
-- Applied by db/migrate.sh inside a transaction it already opened, so the two rules
-- 001_init.sql states hold here too: no BEGIN/COMMIT, and nothing illegal inside a
-- transaction block (CREATE INDEX CONCURRENTLY, CREATE DATABASE, VACUUM).
--
-- Every table in this schema was written for one person. 003's collections_name_key
-- says so in as many words — "this is a local single-user app" — and it is not the only
-- place the assumption is load-bearing rather than incidental: `GET /api/memos` returns
-- the table, `GET /api/reminders` returns every reminder anybody is owed, and Ask
-- retrieves across all transcripts. Put that on a URL two people can reach and each of
-- them is reading the other's memos.
--
-- What this migration adds is an owner, and deliberately not an account. There is no
-- password, no email, no JWT and no session table. An owner is a row holding one secret
-- the browser keeps in a cookie, which makes this a *capability*: whoever presents the
-- token is the owner, and that is the whole of the check. It is worth being blunt about
-- the consequence, because the schema cannot enforce what the design gives away — a
-- leaked token is a leaked account with no way to notice and no password to change. It
-- is the right trade for memos on a hobby deployment and the wrong one for anything a
-- person would be harmed by losing. Rotation is possible (see `token_hash` below); it
-- is the one recovery this design has.

-- --------------------------------------------------------------------------
-- Owners
-- --------------------------------------------------------------------------

-- Needed for gen_random_bytes() in the backfill at the bottom. IF NOT EXISTS for the
-- reason 001_init.sql gives about pg_trgm: this file must stay replayable against a
-- database that already has the extension, because the ledger protects a compose run
-- and not a hand-rolled psql session. pgcrypto ships with postgres:16-alpine.
--
-- Note that this is needed only by the backfill. sha256() is core since Postgres 11 and
-- gen_random_uuid() since 13, so the *running application* needs no extension at all —
-- which matters on a hosted Postgres that may not grant CREATE EXTENSION. If this line
-- ever fails on such a provider, the backfill is what to cut; nothing else here depends
-- on it.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE owners (
    -- The internal identity, and the target of every foreign key below. A UUIDv7 minted
    -- by the API, matching memos.id and collections.id — same reasoning as 001_init.sql,
    -- unchanged: no DEFAULT, so a missing id is a not-null violation rather than a
    -- silently unordered v4.
    id uuid PRIMARY KEY,

    -- SHA-256 of the token the browser holds, lowercase hex, never the token itself.
    --
    -- **Two columns rather than one, and the split is the point.** The obvious schema
    -- makes the browser's secret the primary key and be done with it. That spreads the
    -- secret into every foreign key, into every row a debug log dumps, and into every
    -- `EXPLAIN` somebody pastes into an issue — and it makes rotation impossible, since
    -- rotating would mean rewriting owner_id on every memo. Here the secret appears in
    -- exactly one column of one table and nothing references it, so rotating an owner
    -- whose link was posted somewhere public is an UPDATE of this one field.
    --
    -- Hashed rather than stored, for a property worth having on a free-tier database
    -- somebody else operates: a dump of this table is not a set of working credentials.
    -- Whoever holds it can prove a token they already have belongs to an owner; they
    -- cannot turn the table into access.
    --
    -- A plain fast hash, not bcrypt or argon2, and that is correct here rather than a
    -- shortcut. Those exist to make guessing *low-entropy* human-chosen secrets slow.
    -- This token is 128 bits from a CSPRNG, so there is nothing to guess and a slow hash
    -- would only add latency to a lookup that happens on every single request.
    --
    -- text and not bytea, so PHP's hash('sha256', $token) and this column compare
    -- without either side thinking about binary parameter binding — the encode(..., 'hex')
    -- in the backfill produces exactly the same lowercase hex PHP does.
    token_hash text NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),

    -- When this owner last made a request, maintained by the API on resolve.
    --
    -- This exists for retention and would not otherwise be here. An anonymous identity
    -- is minted for every browser that ever loads the page — including crawlers, link
    -- previewers and somebody's curl — and unlike a signup none of them cost anything to
    -- create, so the table grows with traffic rather than with users. On a free Postgres
    -- capped around half a gigabyte that is the thing that fills it. The
    -- `memo:prune-owners` artisan command uses this column, and the ON DELETE CASCADE on
    -- the foreign keys below is what makes deleting one row here enough.
    --
    -- Deliberately not updated on every request by the database. The API writes it at
    -- most once a day per owner (see App\Http\Middleware\ResolveOwner) — a write on every
    -- request would turn every GET into a read-write transaction and put this table's
    -- churn on the same order as traffic, which is the opposite of the goal.
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

-- The lookup that runs on every request: cookie -> owner. Unique because two owners
-- sharing a token would make the ownership check ambiguous in the one place it must not
-- be, and the API relies on this to answer with a single row rather than the first of
-- several.
CREATE UNIQUE INDEX owners_token_hash_key ON owners (token_hash);

-- The prune's only question: who has not been seen since a cutoff. Partial indexes were
-- considered and rejected — there is no static predicate to build one on, since the
-- cutoff moves every time it runs.
CREATE INDEX owners_last_seen_idx ON owners (last_seen_at);

-- --------------------------------------------------------------------------
-- Backfill: the memos that existed before anybody owned them
-- --------------------------------------------------------------------------

-- Added nullable, filled, then made NOT NULL at the bottom of this file. The three-step
-- dance is unavoidable for a column with no sensible default on a table that already has
-- rows, and doing it inside migrate.sh's single transaction means no window exists where
-- the column is nullable to anything but this script.
ALTER TABLE memos       ADD COLUMN owner_id uuid;
ALTER TABLE collections ADD COLUMN owner_id uuid;

-- One legacy owner adopting everything already in the database, created only if there is
-- anything to adopt.
--
-- The condition is what keeps a fresh deployment clean: `docker compose up` on an empty
-- volume runs every migration including this one, and without the guard every new
-- install would ship with a phantom owner holding zero memos and a claim link in its
-- logs. On an empty database this whole block is a no-op and the NOT NULLs below apply
-- to zero rows.
--
-- The token is generated here and printed once, because that is the only moment it can
-- be: this file stores the hash, so after this transaction commits nobody — including
-- whoever runs the migration — can recover it from the database. Printing a bearer token
-- to a log deserves the flinch it gets, and two things make it acceptable. It is emitted
-- only when pre-existing rows are found, which on any hosted deployment is never; and the
-- alternative is stranding the memos this project was developed against behind a secret
-- that provably does not exist anywhere.
DO $$
DECLARE
    legacy_id    uuid;
    legacy_token text;
BEGIN
    IF EXISTS (SELECT 1 FROM memos) OR EXISTS (SELECT 1 FROM collections) THEN
        -- v4 from gen_random_uuid() rather than the v7 the API mints, and it is the one
        -- place in this schema where that is fine: v7 exists so rows sort by creation
        -- time, and a single row has nothing to sort against.
        legacy_id := gen_random_uuid();

        -- 16 bytes = 128 bits, base64 with the two URL-hostile characters translated and
        -- the padding stripped, so it survives being a path segment without escaping.
        -- This is the same alphabet and length App\Support\OwnerToken produces.
        legacy_token := rtrim(translate(encode(gen_random_bytes(16), 'base64'), '+/', '-_'), '=');

        INSERT INTO owners (id, token_hash)
        VALUES (legacy_id, encode(sha256(convert_to(legacy_token, 'UTF8')), 'hex'));

        UPDATE memos       SET owner_id = legacy_id WHERE owner_id IS NULL;
        UPDATE collections SET owner_id = legacy_id WHERE owner_id IS NULL;

        RAISE NOTICE '';
        RAISE NOTICE '  Existing memos have been assigned to one owner.';
        RAISE NOTICE '  Open this link once to claim them in your browser:';
        RAISE NOTICE '';
        RAISE NOTICE '      /api/claim/%', legacy_token;
        RAISE NOTICE '';
        RAISE NOTICE '  It is shown here and nowhere else — only the hash is stored.';
        RAISE NOTICE '';
    END IF;
END
$$;

ALTER TABLE memos       ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE collections ALTER COLUMN owner_id SET NOT NULL;

-- ON DELETE CASCADE, and unlike collection_id in 003 this one is not a close call. A
-- memo without an owner is not a memo in the fast strip, it is a row no request can ever
-- reach again — every statement in App\Repositories filters on owner_id, so SET NULL
-- would produce permanently invisible rows that still count against a free tier's disk
-- quota. Deleting an owner is also the only way this application deletes in bulk (see
-- the `memo:prune-owners` command), and "delete this owner and everything of theirs" is exactly
-- what it has to mean.
ALTER TABLE memos
    ADD CONSTRAINT memos_owner_id_fkey
    FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE CASCADE;

ALTER TABLE collections
    ADD CONSTRAINT collections_owner_id_fkey
    FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE CASCADE;

-- --------------------------------------------------------------------------
-- A memo may only be filed into its own owner's collection
-- --------------------------------------------------------------------------

-- **The cross-owner hole that scoping every query does not close.** Both tables now carry an
-- owner, and every statement in App\Repositories filters on it -- but `memos.collection_id`
-- points at a collection row without saying whose, so nothing above stops one owner filing
-- their memo into a stranger's folder. The foreign key 003 declared is perfectly satisfied by
-- it: that collection genuinely exists.
--
-- It is worth being precise about why that matters, because the obvious damage does not
-- happen. Nobody would ever *see* the misfiled memo -- the collection's contents are read
-- through `GET /api/memos?collection=`, which is owner-scoped, so it stays invisible to both
-- parties. What breaks is deletion: when the other owner deletes that collection, the SET NULL
-- below fires on a memo they have never heard of, and its owner watches it leave the folder
-- they filed it in for no reason they can observe.
--
-- Enforced here rather than in the repository that writes it. A predicate in
-- MemoRepository::moveToCollection would hold for exactly as long as that stays the only
-- statement writing this column; a constraint holds for the worker, for a future route, and
-- for a hand-rolled psql session. The repository's existing 23503 handler already turns a
-- violation into the 404 it turns an absent collection into, so this needs no new error path
-- -- from the client's side "no such collection" and "not your collection" are the same
-- mistake and get the same answer.
--
-- The UNIQUE is not redundant with the primary key even though `id` alone is already unique.
-- A foreign key must reference a uniquely-constrained set of columns *as named*, and Postgres
-- will not accept the pair on the strength of the PK covering half of it.
ALTER TABLE collections ADD CONSTRAINT collections_id_owner_key UNIQUE (id, owner_id);

ALTER TABLE memos DROP CONSTRAINT memos_collection_id_fkey;

-- `ON DELETE SET NULL (collection_id)`, with the column list, and the list is load-bearing
-- rather than explicit-for-its-own-sake. A bare SET NULL on a composite foreign key nulls
-- *every* referencing column -- here that is `owner_id` as well, which is NOT NULL, so
-- deleting any collection would fail outright and take 003's "deleting a folder returns its
-- memos to the fast strip" with it. The column list is Postgres 15+; this schema is on 16.
ALTER TABLE memos
    ADD CONSTRAINT memos_collection_id_fkey
    FOREIGN KEY (collection_id, owner_id) REFERENCES collections (id, owner_id)
    ON DELETE SET NULL (collection_id);

-- --------------------------------------------------------------------------
-- Reminders carry the owner too
-- --------------------------------------------------------------------------

-- Denormalized from the memo, which is the one piece of redundancy in this migration and
-- the only one that earns its keep.
--
-- Reminders already cascade from memos, so ownership is derivable by a join and the
-- correctness argument needs no column. The cost query is what does. `pending` asks "what
-- is due and undelivered", the frontend polls it, and 003's reminders_due_idx answers it
-- from a partial index in remind_at order. Scope that by joining memos and the index
-- still returns every owner's due reminders in time order — the LIMIT then applies after
-- the join, so one owner with nothing pending pays to walk everybody else's. Indexed on
-- (owner_id, remind_at) instead, the same question is a range scan over one owner's rows.
--
-- What redundancy normally costs is the chance of the two copies disagreeing, and here
-- there is no path to it: ReminderRepository::insert derives this column from the memo
-- row in the same INSERT ... SELECT that checks the memo belongs to the caller, and no
-- operation in this application moves a memo between owners. If one is ever added, it
-- must update this column in the same statement.
ALTER TABLE reminders ADD COLUMN owner_id uuid;

UPDATE reminders r SET owner_id = m.owner_id FROM memos m WHERE m.id = r.memo_id;

-- Safe even though reminders has no rows in any current deployment: an empty table makes
-- this a no-op rather than a failure, and the column has to end up NOT NULL either way.
ALTER TABLE reminders ALTER COLUMN owner_id SET NOT NULL;

ALTER TABLE reminders
    ADD CONSTRAINT reminders_owner_id_fkey
    FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE CASCADE;

-- --------------------------------------------------------------------------
-- Indexes: every list query is now owner-first
-- --------------------------------------------------------------------------

-- The list endpoint's ordering, scoped. This supersedes 001's memos_created_idx for
-- every query the API issues, because there is no longer a request that reads memos
-- across owners.
--
-- memos_created_idx is deliberately kept rather than dropped, and not out of caution:
-- ai/memo_ai/costs.py aggregates over the whole table on purpose — "what would this cost
-- per 1000 memos" is a question about the deployment and not about one person — and those
-- queries have no owner to lead with.
CREATE INDEX memos_owner_created_idx ON memos (owner_id, created_at DESC);

-- Both readings of collection_id, scoped: one collection's memos, and the fast strip's
-- `collection_id IS NULL`. Replaces 003's memos_collection_idx, which is dropped rather
-- than kept because its leading column is now always preceded by an equality on owner_id
-- — every query that could have used it can use this one, and an unused index is write
-- amplification on the table this app writes most.
--
-- 003 measured which of the two readings actually uses the index and found the fast strip
-- falls back to a created_at scan when most memos are unfiled. That measurement was taken
-- against a single-owner table and is not carried forward here: with rows split across
-- owners the planner is choosing between different shapes, and restating a number that
-- was not re-measured would be worse than leaving it open.
CREATE INDEX memos_owner_collection_idx ON memos (owner_id, collection_id, created_at DESC);

DROP INDEX memos_collection_idx;

-- The collections list, scoped. Replaces 003's collections_created_idx for the same
-- reason, and that one is dropped too — nothing reads collections across owners.
CREATE INDEX collections_owner_created_idx ON collections (owner_id, created_at DESC);

DROP INDEX collections_created_idx;

-- The pending-reminder poll, scoped. Partial on undelivered for the reason 003 gives:
-- delivered rows are the ones that accumulate and none of them can ever match.
CREATE INDEX reminders_owner_due_idx ON reminders (owner_id, remind_at) WHERE delivered_at IS NULL;

DROP INDEX reminders_due_idx;

-- --------------------------------------------------------------------------
-- Collection names are unique per owner, not globally
-- --------------------------------------------------------------------------

-- **The sharpest bug in a naive port of this schema, and it is silent.** 003 declares
-- collections_name_key over `lower(btrim(name))` alone, on the stated grounds that this is
-- a single-user app. Left as it is, the second person to create a collection called "Work"
-- gets CollectionRepository's 23505 handler and the sentence it was given — "You already
-- have a collection called Work" — which is a lie: they have no collections at all, and
-- nothing they can see explains the refusal.
--
-- owner_id leads, so the index also serves `WHERE owner_id = ?` lookups, though nothing
-- currently issues one that would prefer it over collections_owner_created_idx.
DROP INDEX collections_name_key;

CREATE UNIQUE INDEX collections_name_key ON collections (owner_id, lower(btrim(name)));

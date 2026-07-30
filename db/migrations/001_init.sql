-- 001_init.sql — the memos aggregate.
--
-- Applied by db/migrate.sh, which wraps this file and its schema_migrations row
-- in a single psql --single-transaction invocation. Two rules follow from that
-- and hold for every file added to this directory:
--
--   * no BEGIN/COMMIT in here — psql has already opened the transaction block
--   * nothing that is illegal inside one: CREATE INDEX CONCURRENTLY,
--     CREATE DATABASE, VACUUM
--
-- One table, not two. Queue state (status, attempts, locked_at,
-- next_attempt_at) lives on the memo row rather than in a separate jobs table,
-- so a memo and the work it owes are created by the same INSERT. There is no
-- window where one exists without the other and nothing to reconcile after a
-- crash mid-write.

-- Required by memos_trgm_idx below. IF NOT EXISTS because this file must stay
-- replayable against a database that already has the extension — the ledger
-- normally prevents a second run, but a hand-rolled psql session is not owed
-- that protection. pg_trgm ships with postgres:16-alpine, and POSTGRES_USER is
-- the superuser initdb created, so no privilege wiring is needed here; the
-- extension also declares trusted = true, which is what keeps this line working
-- if DATABASE_URL is ever repointed at a non-superuser role.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE memos (
    -- No DEFAULT, deliberately. The API generates a UUIDv7 itself so the 201
    -- response can carry the id without a second round trip and rows sort
    -- naturally by time (MEMO-06). Do not "complete" this with
    -- DEFAULT gen_random_uuid(): that emits v4, whose ordering is arbitrary
    -- (verified), and it would apply silently to any INSERT omitting the column
    -- — turning a missing id in the write path into unordered rows rather than
    -- the not-null violation that would have caught it.
    id            uuid PRIMARY KEY,
    source        text NOT NULL CHECK (source IN ('voice','text')),
    status        text NOT NULL CHECK (status IN ('queued','processing','ready','failed')),

    -- NULL means transcription is still owed. A text memo is inserted with its
    -- transcript already set, which is how the worker tells the two apart
    -- without a second flag: `transcript IS NULL` decides whether STT runs.
    transcript    text,

    title         text,
    summary       text,

    -- NOT NULL DEFAULT '{}' is load-bearing, not tidiness: array_to_tsvector()
    -- in search_vector below raises on a NULL array, so a nullable tags column
    -- would fail every INSERT that omitted it.
    --
    -- The elements are constrained too, by that same function: a NULL or
    -- empty-string element aborts the write with `lexeme array may not contain
    -- nulls` / `... empty strings`, naming neither the column nor the table.
    -- That message is the symptom to look for when a write from MEMO-21's tag
    -- normalizer fails. A CHECK constraint spelling the invariant out is not
    -- worth adding — verified: generated columns are evaluated ahead of CHECK
    -- constraints, so array_to_tsvector always raises first and the constraint
    -- never fires.
    tags          text[] NOT NULL DEFAULT '{}',

    category      text,
    audio_path    text,
    audio_mime    text,
    duration_ms   integer,

    -- Retry bookkeeping for the worker's claim loop.
    attempts      integer NOT NULL DEFAULT 0,
    locked_at     timestamptz,

    -- NOT NULL with a default, again load-bearing. The claim predicate compares
    -- this against now(); NULL is not less than anything, so a nullable column
    -- would match zero rows and nothing would ever be processed.
    next_attempt_at timestamptz NOT NULL DEFAULT now(),

    last_error    text,

    -- Separate from last_error on purpose. Enrichment is optional (no
    -- ANTHROPIC_API_KEY is a supported configuration), so its failure must not
    -- read as a failure of the memo itself: the row still reaches 'ready'.
    enrichment_error text,
    enriched_at   timestamptz,

    stt_provider  text,
    stt_model     text,

    -- Millionths of a dollar, not cents. A real 20-second memo costs a fraction
    -- of a cent, which integer cents would round to 0 and make the column
    -- useless for exactly the rows there are most of.
    cost_micro_usd bigint,

    created_at    timestamptz NOT NULL DEFAULT now(),

    -- The default covers the INSERT and nothing more. Postgres has no
    -- ON UPDATE CURRENT_TIMESTAMP, and verified here: an UPDATE leaves this at
    -- its insert value. Two runtimes write this table, so it needs either an
    -- explicit `updated_at = now()` on every UPDATE or a BEFORE UPDATE trigger.
    -- MEMO-06 owns that choice and this migration installs no trigger, so until
    -- then the column is insert-only and must not be trusted as a change clock.
    updated_at    timestamptz NOT NULL DEFAULT now(),

    -- STORED, so it is computed on write and needs no trigger and no second
    -- write path. Three constraints shaped this expression, each verified by
    -- watching the obvious version get rejected:
    --
    --   * a STORED generated column requires an IMMUTABLE expression.
    --     Single-argument to_tsvector() reads default_text_search_config and is
    --     only STABLE, so it is refused. The two-argument form with a literal
    --     config resolves at DDL time and is immutable.
    --   * folding tags in with array_to_string() fails the same check —
    --     array_to_string is STABLE. array_to_tsvector() is immutable and
    --     produces the lexemes directly.
    --   * coalesce() on every input. tsvector concatenation with a NULL operand
    --     yields NULL, so one absent title would void the whole vector and drop
    --     the row out of search entirely.
    --
    -- The ' ' separators are structural, not formatting: without them an absent
    -- summary would butt title against transcript and index the join as one
    -- token. Two consequences to carry into the search endpoint (MEMO-12):
    --
    --   * array_to_tsvector emits lexemes with no positions, so a memo matched
    --     only by a tag scores exactly 0 under ts_rank_cd — verified, against
    --     0.1 for a transcript match on the same row. Plain ts_rank does give it
    --     a nonzero score. Rank with ts_rank, or tag-only hits sort dead last.
    --   * the column is part of SELECT *, and it is the largest thing on the
    --     row. The API enumerates the columns it returns rather than shipping
    --     this to the client, and an INSERT naming it fails outright with
    --     "cannot insert a non-DEFAULT value into column search_vector".
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(transcript,''))
        || array_to_tsvector(coalesce(tags,'{}'::text[]))
    ) STORED
);

-- Full-text search. GIN over the stored vector, so the index is maintained by
-- the same write that computes the column.
CREATE INDEX memos_search_idx ON memos USING gin (search_vector);

-- The list endpoint's only ordering: newest first, unfiltered.
CREATE INDEX memos_created_idx ON memos (created_at DESC);

-- The worker's claim query: status = 'queued' AND next_attempt_at <= now().
-- Leading equality column, then the range column — the order that lets one
-- index scan satisfy both predicates.
CREATE INDEX memos_claim_idx ON memos (status, next_attempt_at);

-- Substring matching on transcripts, for the two cases the tsvector genuinely
-- cannot reach. Stemming is not one of them — 'meet' does match 'meetings', and
-- the parser splits 'meeting-room' into 'meeting-room'/'meet'/'room', so that
-- matches too. What it misses, both verified against this schema:
--
--   * a partial word. Searching 'dentis' does not match a memo saying
--     'dentist': the query term is stemmed, not prefix-expanded.
--   * a run-together token. 'meetingroom' indexes as a single lexeme, so
--     'meeting' never reaches it.
--
-- Both are reachable by LIKE '%...%', which has no other index available.
CREATE INDEX memos_trgm_idx ON memos USING gin (transcript gin_trgm_ops);

-- 002_updated_at.sql — the BEFORE UPDATE trigger that keeps updated_at honest.
--
-- Same two rules as every file in this directory, because db/migrate.sh wraps it
-- and its ledger row in one psql --single-transaction call: no BEGIN/COMMIT in
-- here, and nothing illegal inside a transaction block.
--
-- 001_init.sql left this open deliberately. Postgres has no
-- ON UPDATE CURRENT_TIMESTAMP, and `updated_at timestamptz NOT NULL DEFAULT
-- now()` covers the INSERT and nothing else — verified again on this schema
-- before writing the trigger: an UPDATE that changes `status` leaves updated_at
-- at its insert value. So the column needed either an explicit
-- `updated_at = now()` on every UPDATE or a trigger. MEMO-06 owns that choice.
--
-- The choice is the trigger, and the deciding argument is that two runtimes
-- write this table. The API is one; the Python worker (MEMO-08) is the other,
-- and its claim statement is already specified as
--
--     UPDATE memos SET status='processing', locked_at=now(), attempts=attempts+1
--     WHERE id = (...) RETURNING *
--
-- which does not set updated_at. The convention would therefore have been broken
-- by the very next task to touch the table, in a different language, by an author
-- with no reason to read the PHP repository. A trigger cannot be forgotten by a
-- new writer, a psql session, or a future `ai-api`; a convention in a code review
-- checklist can. The failure mode it prevents is also the quiet kind — a stale
-- timestamp reads as a valid one, so nothing surfaces until someone trusts the
-- column and is wrong.
--
-- What this column is NOT good for, so nobody builds on it by mistake: it is not
-- a delta cursor. MEMO-18 rules a `GET /api/memos?since=` out on the frontend
-- side and polls the whole visible page instead, but the reason belongs next to
-- the column too — now() is transaction-start time, so a row whose write
-- transaction started before a poll read and committed after it carries an
-- updated_at the poller has already passed, and the row is silently skipped. A
-- trigger makes the column truthful about "when was this row last written"; it
-- does not make it monotonic, and a delta feed needs a sequence, not a clock.
--
-- Rejected: `WHEN (OLD.* IS DISTINCT FROM NEW.*)` on the trigger, the usual trick
-- for not bumping the timestamp on a no-op UPDATE. It is actively wrong on this
-- table. Verified here: in a BEFORE trigger the STORED generated column
-- search_vector is not yet computed, so NEW.search_vector is NULL while
-- OLD.search_vector holds the previous vector. The rows therefore always compare
-- as distinct, the guard never suppresses anything, and it reads like a working
-- optimisation. A no-op UPDATE on this table is not a case worth optimising for
-- anyway — nothing in the design issues one.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- CREATE OR REPLACE TRIGGER, not a bare CREATE TRIGGER: this file must stay
-- replayable against a database that already has the trigger, for the same reason
-- 001_init.sql uses CREATE EXTENSION IF NOT EXISTS. The ledger normally prevents a
-- second run, but a hand-rolled psql session is not owed that protection.
-- Triggers have no IF NOT EXISTS form; OR REPLACE arrived in Postgres 14 and the
-- image is pinned to 16.
CREATE OR REPLACE TRIGGER memos_set_updated_at
    BEFORE UPDATE ON memos
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

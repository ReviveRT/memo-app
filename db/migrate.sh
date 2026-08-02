#!/bin/sh
# Numbered SQL migrations, safe to re-run. Invoked by the one-shot `migrate`
# service as `sh /db/migrate.sh`, with ./db mounted read-only at /db.
#
# Why not docker-entrypoint-initdb.d: it fires only while the pgdata volume is
# still empty and is silently skipped on every boot after that, so a second
# migration would never apply.
#
# Why the ledger is load-bearing: a plain second `docker compose up` restarts the
# completed one-shot container and replays every file it finds. With no record of
# what already ran, 001_init.sql applies twice and psql exits 3 with
# `ERROR: relation "memos" already exists` — which also stops `api`, because it
# gates on this service via `condition: service_completed_successfully`.

set -eu

# Byte-wise collation so the glob below orders files identically whatever locale
# the image happens to carry.
LC_ALL=C
export LC_ALL

: "${DATABASE_URL:?DATABASE_URL is not set}"

# Resolves to /db/migrations under compose and to ./db/migrations when the script
# is run straight from a checkout, so this needs no extra wiring in either case.
case "$0" in
    */*) script_dir=${0%/*} ;;
    *)   script_dir=. ;;
esac
migrations_dir="${MIGRATIONS_DIR:-$script_dir/migrations}"

if [ ! -d "$migrations_dir" ]; then
    echo "migrate: not a directory: $migrations_dir" >&2
    exit 1
fi

# The connection string goes through -d rather than as a trailing dbname
# argument, so nothing here is positional and option parsing cannot depend on
# whether the platform's getopt permutes arguments that follow one.
#
# --no-psqlrc so a stray ~/.psqlrc cannot switch on ON_ERROR_ROLLBACK or reshape
# the output parsed below. ON_ERROR_STOP=1 because psql otherwise carries on past
# a failed statement and exits 0 — which would record a broken migration as
# applied, the one outcome the ledger must never contain.
psql_run() {
    psql --no-psqlrc --quiet -v ON_ERROR_STOP=1 -d "$DATABASE_URL" "$@"
}

# client_min_messages is scoped to this one call, in a subshell: CREATE TABLE IF
# NOT EXISTS emits `NOTICE: relation "schema_migrations" already exists,
# skipping` on every run after the first, which --quiet does not suppress and
# which reads like a fault in otherwise clean `docker compose up` output.
# Migrations keep their own notices.
(
    PGOPTIONS='-c client_min_messages=warning'
    export PGOPTIONS
    psql_run -c 'CREATE TABLE IF NOT EXISTS schema_migrations (
        filename   text        PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT now()
    );'
)

# -A -t: unaligned and tuples-only, so this is exactly one filename per line with
# no header, no padding and no row count to strip.
applied=$(psql_run -A -t -c 'SELECT filename FROM schema_migrations;')

count=0
# Filename order is lexicographic, so numeric prefixes must be zero-padded to a
# fixed width: an unpadded 10_x.sql would sort ahead of 9_x.sql.
for path in "$migrations_dir"/*.sql; do
    # An unmatched glob stays literal rather than expanding to nothing, so this
    # guard is what stops a directory holding no .sql files from handing psql a
    # path named `*.sql`.
    [ -f "$path" ] || continue

    filename=${path##*/}

    if printf '%s\n' "$applied" | grep -Fxq "$filename"; then
        continue
    fi

    echo "migrate: applying $filename"

    # The filename is embedded as a quoted literal with its own single quotes
    # doubled, not passed as a psql variable: -c is handed to the server as-is,
    # so a :'var' in it arrives verbatim and fails with `syntax error at or
    # near ":"`. Interpolation only happens for input psql itself lexes (-f).
    escaped=$(printf '%s' "$filename" | sed "s/'/''/g")

    # One transaction spanning both the migration and its ledger row: psql runs
    # -f and -c in the order given, and --single-transaction wraps the pair, so
    # the INSERT commits with the DDL or neither lands. Two consequences for
    # anything added to db/migrations: no file may carry its own BEGIN/COMMIT,
    # and none may use a statement that is illegal inside a transaction block
    # (CREATE INDEX CONCURRENTLY, CREATE DATABASE, VACUUM). Keeping the INSERT
    # in its own -c rather than appending it to the file's text also keeps the
    # two errors apart: a migration missing its final semicolon fails as itself
    # instead of silently merging into this statement. Plain INSERT rather than
    # ON CONFLICT DO NOTHING is deliberate — a duplicate key here would mean the
    # check above is broken, and failing loudly beats recording it quietly.
    # pg_advisory_xact_lock first, and it is inside the same --single-transaction as the
    # migration, so it is held until that transaction commits and released by it.
    #
    # It is here because deploy/entrypoint.sh runs this script at container boot rather than
    # as compose's gated one-shot service. Two containers starting together would otherwise
    # both find a migration unapplied and both run it, and the loser would fail on whatever
    # its DDL had already done -- `relation "owners" already exists` -- rather than on the
    # ledger's primary key, which is a much less legible error.
    #
    # **What this does and does not guarantee, since a half-promise is worse than none.** It
    # serialises the writes, so the database is never touched by two migrators at once and
    # cannot end up half-applied. It does *not* make the loser exit 0: that container read the
    # ledger before blocking on this lock, so it still tries to apply a file the winner has
    # since recorded, and it still exits non-zero. What it converts is the failure's shape --
    # from corrupt-or-arbitrary to a clean "already exists" on a container the platform will
    # restart, whose next boot reads the updated ledger and starts normally.
    #
    # The key is an arbitrary constant, shared by every instance of this application and by
    # nothing else. Advisory locks live in one cluster-wide namespace, so the number matters
    # only in that two unrelated applications picking the same one would block each other.
    # Wrapped in a DO block rather than issued as a bare SELECT, and that is about output
    # rather than semantics: --quiet does not suppress a result set, so `SELECT
    # pg_advisory_xact_lock(...)` prints a header, a blank value and "(1 row)" before every
    # migration -- three lines of noise per file in what this script works hard to keep a
    # clean `docker compose up`. PERFORM inside DO returns nothing.
    psql_run --single-transaction \
        -c 'DO $$ BEGIN PERFORM pg_advisory_xact_lock(4021979); END $$;' \
        -f "$path" \
        -c "INSERT INTO schema_migrations (filename) VALUES ('$escaped');"

    count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
    echo "migrate: schema up to date, nothing to apply"
else
    echo "migrate: applied $count migration(s)"
fi

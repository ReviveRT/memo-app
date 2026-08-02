#!/bin/sh
# Bring the schema up to date, then hand over to the web server.
#
# Compose has a one-shot `migrate` service and gates `api` on it with
# `condition: service_completed_successfully`. A single container has nowhere to put a second
# service, and free platforms mostly have no release-command hook either -- so the migration
# runs here, at boot, before anything can serve a request against a schema it does not match.
#
# The ordering is the point: `exec` is only reached if migrate.sh exited 0. A failed migration
# therefore kills the container instead of starting an API on a half-migrated database, which
# is the failure mode worth being loud about -- the alternative is 500s from every route with
# nothing in the log to say why.

set -eu

if [ -n "${DATABASE_URL:-}" ]; then
    echo "memo: applying migrations"
    sh /db/migrate.sh
else
    # Not fatal, because there is one legitimate way to reach this: a platform building or
    # health-checking the image before its database is attached. The API's own /api/health
    # answers 503 while the database is unreachable, so this cannot be mistaken for a working
    # deployment for long.
    echo "memo: DATABASE_URL is not set, skipping migrations" >&2
fi

exec "$@"

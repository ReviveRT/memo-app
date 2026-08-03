#!/bin/bash
# Bring the schema up to date, start the worker if this deployment runs one, then hand over
# to the web server.
#
# Compose has a one-shot `migrate` service and gates `api` on it with
# `condition: service_completed_successfully`. A single container has nowhere to put a second
# service, and free platforms mostly have no release-command hook either -- so the migration
# runs here, at boot, before anything can serve a request against a schema it does not match.
#
# The ordering is the point: nothing below the migration is reached unless it exited 0. A
# failed migration therefore kills the container instead of starting an API on a
# half-migrated database, which is the failure mode worth being loud about -- the alternative
# is 500s from every route with nothing in the log to say why.
#
# **bash rather than sh**, and it is load-bearing rather than habit: the two-process path
# below needs `wait -n`, which dash does not have. The base image is Debian and ships bash.

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

# --- The single-process path -------------------------------------------------
#
# First, because it is the one that should stay boring. `exec` puts the web server at PID 1
# where it receives SIGTERM directly, which is what the container runtime expects and what
# every deployment that runs its worker elsewhere gets.
if [ "${RUN_WORKER:-true}" != "true" ]; then
    echo "memo: RUN_WORKER is not 'true', starting the web server alone"
    exec "$@"
fi

# The same path, for the case the migration step above deliberately tolerates.
#
# This guard is load-bearing and was added after the two-process path broke the promise made
# thirty lines up. `memo_ai` refuses to start without a database -- "Nothing in this package
# runs without a database" -- and exits 2. Because either child dying takes the container
# down, an image started before its database is attached went from "API up, /api/health
# answers 503" to a crash loop, which is precisely the outcome the comment above says is not
# worth having. Reproduced before fixing: exit 2, with the web server SIGTERMed a fraction of
# a second after it began listening.
#
# A platform building or health-checking a service before its database exists is a real
# sequence -- Render's blueprint creates both and does not promise an order -- so the honest
# response is to serve the API and let /api/health report the truth, not to refuse to boot.
if [ -z "${DATABASE_URL:-}" ]; then
    echo "memo: DATABASE_URL is not set, starting the web server without a worker" >&2
    exec "$@"
fi

# --- Two processes, one container --------------------------------------------
#
# From here the shell stays as PID 1 and supervises, which is a job worth doing properly
# because getting it wrong is invisible until it matters. Three things have to hold:
#
#   1. **SIGTERM reaches both children.** PID 1 gets the signal on `docker stop` and on every
#      platform's deploy-and-replace; children get nothing unless it is forwarded. Without
#      the trap, the worker is SIGKILLed at the end of the grace period -- mid-job, with its
#      memo left in `processing` for the reaper to take back some time later. The worker
#      handles SIGTERM specifically so the job in flight finishes and writes its result
#      (memo_ai/worker/__main__.py), and that handler is only reachable if the signal arrives.
#
#   2. **Either child dying takes the container down.** `wait -n` returns on the first exit,
#      not the last. A container whose web server has crashed but whose worker still holds
#      the process alive is the worst outcome available: the platform's health check fails,
#      the platform restarts nothing because the container is still up, and the logs show a
#      worker politely polling an empty queue.
#
#   3. **The exit status is the dead child's**, so a crash loop is legible in the platform's
#      dashboard rather than showing as a clean exit 0.
declare -i worker_pid=0 server_pid=0

# Not `exec` -- the shell has to stay alive to supervise. `PYTHONUNBUFFERED` for the same
# reason ai/Dockerfile sets it: stdout to a pipe is block-buffered, so without it the last
# log lines before a hard exit are lost exactly when they are worth reading.
PYTHONUNBUFFERED=1 PYTHONPATH=/opt/memo-ai \
    /opt/memo-ai/venv/bin/python -m memo_ai.worker &
worker_pid=$!
echo "memo: worker started (pid ${worker_pid}, stt=${STT_PROVIDER:-groq}, enrich=${ENRICH_PROVIDER:-none})"

"$@" &
server_pid=$!

# Forward, then let the `wait` below reap. `|| true` because a child that has already exited
# makes kill(1) fail, and `set -e` would turn tidying up into a nonzero exit of its own.
shutdown() {
    echo "memo: shutting down"
    kill -TERM "${worker_pid}" "${server_pid}" 2>/dev/null || true
}
trap shutdown TERM INT

# `set -e` would abort here the moment the first child exits nonzero, skipping the shutdown
# of the other one and leaving it to be SIGKILLed. The point of this block is to run *after*
# a failure, so the flag comes off for it.
set +e
wait -n "${worker_pid}" "${server_pid}"
first_status=$?

# Whichever went first, the other one goes too -- see (2) above.
shutdown
wait
exit "${first_status}"

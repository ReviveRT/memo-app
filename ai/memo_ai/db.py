"""The Postgres connection, and the one setting on it that carries real weight."""

import socket

import psycopg

from memo_ai.config import Settings

# The correct mechanism, and honestly not a proven bound. api/Dockerfile records
# the measurement behind that: against a blackholed address from this stack, the
# connect fails after 30s regardless of what libpq is told, because Docker
# Desktop's network stack imposes it. Kept because it is the right knob and does
# apply on hosts that are not Docker Desktop -- and because the worker's reconnect
# loop makes even the 30s case survivable rather than terminal.
CONNECT_TIMEOUT_SECONDS = 5


def connect(settings: Settings, role: str = "worker") -> psycopg.Connection:
    """
    ``autocommit=True`` is the line that makes MEMO-08's claim a *committed*
    claim, and it is not a performance setting.

    psycopg's default is autocommit off, where the first ``execute()`` opens a
    transaction that stays open until ``commit()``. The claim and the slow work
    that follows it would then sit inside one transaction, and that shape was
    reproduced with two psql sessions against this schema before this file
    existed:

      * claim held open, worker killed -> the row rolls back to
        ``status='queued', attempts=0, locked_at=NULL``. Which makes ``locked_at``
        and MEMO-16's reaper dead code, hides ``processing`` from the API
        entirely, and loses the attempt count that a poison memo needs in order
        to terminate at 3 instead of looping forever.
      * claim committed, worker killed -> the row is left
        ``status='processing', attempts=1, locked_at`` set. Which is exactly what
        the reaper looks for.

    With autocommit on, every statement in memo_ai/memos.py is its own
    transaction. The claim therefore commits before ``transcribe()`` is called,
    and the result write is a second short one, with nothing open in between.
    That is the whole of the "two commit points" mechanism -- there is no explicit
    ``BEGIN`` anywhere in this package, and adding one would quietly undo it.

    ``application_name`` costs nothing and answers "are both replicas actually
    working?" from ``pg_stat_activity`` without adding a healthcheck or a metrics
    endpoint. The hostname is the container id under compose.

    ``role`` is what that name is built from, and it exists because this is no
    longer only the worker's door to the database: ``python -m memo_ai.costs``
    (MEMO-22) runs its aggregates over the same connection settings and would
    otherwise appear in ``pg_stat_activity`` as a third replica. The default keeps
    every existing caller writing the name it always wrote.
    """
    return psycopg.connect(
        settings.database_url,
        autocommit=True,
        application_name=f"memo-{role}@{socket.gethostname()}",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )

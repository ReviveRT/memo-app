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

    ``role`` is what keeps that answer readable now that two services share this
    function. ``ai-api`` (MEMO-24) opens short connections of its own against the
    same database, and without a name of their own they would appear in
    ``pg_stat_activity`` as workers -- so "are both replicas working?" would be
    answered by counting three things and getting four. It is a parameter with a
    default rather than a field on ``Settings`` because it is a property of the
    entrypoint, not of the environment: nothing in a ``.env`` could know or should
    decide which of the two opened a connection.
    """
    return psycopg.connect(
        settings.database_url,
        autocommit=True,
        application_name=f"memo-{role}@{socket.gethostname()}",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )

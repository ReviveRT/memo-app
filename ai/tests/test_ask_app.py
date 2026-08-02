"""
The HTTP surface of ai-api: the framing, the validation, and what /health reports.

Driven through ``TestClient`` against the real routes rather than by calling the
functions, because most of what is worth checking here is FastAPI's wiring rather
than ours -- that pydantic refuses a question before the model is touched, that the
media type is what the PHP proxy expects, that the healthcheck's status code follows
the report rather than the process being up.

``db.connect`` is the one thing patched. There is no Postgres in this suite (the same
constraint the PHP feature tests have), and what the fake connection returns is
already covered by tests/test_ask_retrieval.py.
"""

import json
from datetime import UTC, datetime
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from memo_ai import db
from memo_ai.ask import app as ask_app
from memo_ai.ask.app import MAX_QUESTION_CHARS, create_app
from memo_ai.config import Settings
from tests.support import FakeConnection, RecordingAskModel

CREATED_AT = datetime(2026, 7, 31, 12, 0, 0, 123456, tzinfo=UTC)

SETTINGS = Settings.from_env({"DATABASE_URL": "postgresql://memo:memo@db:5432/memo"})


class ManagedConnection(FakeConnection):
    """A FakeConnection that also works as the context manager ``db.connect`` returns."""

    def __enter__(self) -> "ManagedConnection":
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


def connection(lexemes=("dentist",), rows=None) -> ManagedConnection:
    return ManagedConnection(
        rows=[
            [(lexeme,) for lexeme in lexemes],
            [
                {
                    "id": UUID("01900000-0000-7000-8000-000000000001"),
                    "title": "Call the dentist",
                    "created_at": CREATED_AT,
                    "transcript_chars": 21,
                    "excerpt": "Call the dentist.",
                }
            ]
            if rows is None
            else rows,
        ]
    )


@pytest.fixture
def database(monkeypatch):
    """Hands every ``db.connect`` the same fake, and records the roles asked for."""
    state = {"connection": connection(), "roles": [], "error": None}

    def connect(_settings, role="worker"):
        state["roles"].append(role)

        if state["error"] is not None:
            raise state["error"]

        return state["connection"]

    monkeypatch.setattr(db, "connect", connect)
    # app.py imported the module, not the function, so patching `db.connect` is
    # enough -- asserted here rather than assumed, because patching the wrong one
    # produces a suite that passes against a real database it cannot reach.
    assert ask_app.db is db

    return state


def client(model: RecordingAskModel) -> TestClient:
    return TestClient(create_app(SETTINGS, model))


def events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line]


# --- POST /ask ---------------------------------------------------------------


def test_the_answer_is_one_json_object_per_line(database):
    model = RecordingAskModel(chunks=("You should ", "call [1]."))

    with client(model) as http:
        response = http.post("/ask", json={"question": "what about the dentist"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    assert [event["type"] for event in events(response)] == [
        "sources",
        "token",
        "token",
        "done",
    ]


def test_the_answer_is_never_stored_by_a_cache(database):
    with client(RecordingAskModel()) as http:
        response = http.post("/ask", json={"question": "what about the dentist"})

    assert "no-store" in response.headers["cache-control"]


def test_the_lines_are_ascii_whatever_the_memo_was_written_in(database):
    """
    ``ensure_ascii`` is left on, so a Russian memo's excerpt travels as escapes.
    The line is then ASCII whatever the proxy in front of it assumes about
    encoding -- and PHP passes these through as bytes.
    """
    database["connection"] = connection(
        rows=[
            {
                "id": UUID("01900000-0000-7000-8000-000000000001"),
                "title": "Встреча",
                "created_at": CREATED_AT,
                "transcript_chars": 7,
                "excerpt": "Встреча",
            }
        ]
    )

    with client(RecordingAskModel()) as http:
        response = http.post("/ask", json={"question": "what about the meeting"})

    assert response.content.isascii()
    assert events(response)[0]["sources"][0]["title"] == "Встреча"


def test_the_model_is_loaded_when_the_service_starts_rather_than_on_the_first_question(
    database,
):
    """
    Resident, which is the opposite of the enricher and the whole reason ai-api is
    its own service: a person is watching, so the first question of the day must not
    also be the one that waits out a model load.
    """
    model = RecordingAskModel()

    with client(model):
        pass

    assert model.loads == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "a"},
        {"question": "a" * (MAX_QUESTION_CHARS + 1)},
        {"question": 12},
        {"question": None},
    ],
)
def test_a_question_of_the_wrong_shape_is_refused_before_the_model_is_touched(database, body):
    model = RecordingAskModel()

    with client(model) as http:
        response = http.post("/ask", json=body)

    assert response.status_code == 422
    assert model.calls == []


def test_a_database_that_is_not_answering_arrives_as_an_error_event(database):
    """
    Not a 503, and it cannot be: `_lines` only runs once the response has begun, so
    200 is already committed. Reported inside the stream instead, which is also what
    makes it visible rather than arriving as a truncated body.
    """
    database["error"] = psycopg.OperationalError("connection refused")

    with client(RecordingAskModel()) as http:
        response = http.post("/ask", json={"question": "what about the dentist"})

    assert response.status_code == 200

    (event,) = events(response)

    assert event["type"] == "error"
    assert "db container" in event["message"]


def test_the_ask_connection_is_named_apart_from_the_workers(database):
    """
    `pg_stat_activity` is how "are both replicas working?" is answered on this
    project, and ai-api opening connections that looked like workers would make the
    answer three things counted as four.
    """
    with client(RecordingAskModel()) as http:
        http.post("/ask", json={"question": "what about the dentist"})

    assert database["roles"] == ["ask"]


# --- GET /health -------------------------------------------------------------


def test_health_is_200_only_when_a_question_could_actually_be_answered(database):
    with client(RecordingAskModel(state="ready")) as http:
        response = http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ready": True, "model": "ready", "database": True}


def test_health_reports_503_while_the_model_is_still_loading(database):
    """
    What the compose healthcheck waits on. A socket that accepts connections is not
    the same as a service that can answer, and `healthy` should mean the second.
    """
    with client(RecordingAskModel(state="loading")) as http:
        response = http.get("/health")

    assert response.status_code == 503
    assert response.json()["model"] == "loading"


def test_health_reports_the_database_separately_from_the_model(database):
    """
    Both halves, because the remedies differ: a model that will not load is an image
    problem and a database that will not answer is a stack problem.
    """
    database["error"] = psycopg.OperationalError("connection refused")

    with client(RecordingAskModel(state="ready")) as http:
        response = http.get("/health")

    assert response.status_code == 503
    assert response.json() == {"ready": False, "model": "ready", "database": False}

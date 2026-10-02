"""Audit events: committed with the model in SQL, then copied to PostgreSQL by the exporter.

The tests marked `postgres` need a database at the Alembic head (revision 0005 creates calc."AuditEvent"):

    AUDIT_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/gea_test python -m pytest tests/test_audit.py

The login only needs to be a member of role calc_audit. Without the variable those tests are skipped.
"""
import os
import uuid

import psycopg
import pytest
import snowflake.connector

from calculation_api.audit_export import PostgresAudit, export_pending, postgres_connection
from calculation_api.bootstrap import seed
from calculation_api.gateway import SnowflakeGateway
from calculation_api.repository import Repository


@pytest.fixture
def sink():
    url = os.getenv("AUDIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AUDIT_TEST_DATABASE_URL to a PostgreSQL database at the Alembic head for the audit export tests.")
    # calc."AuditEvent" is append-only, so nothing is cleaned up: every test uses new event ids.
    with postgres_connection(url) as connection:
        yield PostgresAudit(connection)


@pytest.mark.postgres
def test_outbox_retry_and_duplicate_delivery(emulator, sink):
    with snowflake.connector.connect(**emulator) as connection:
        seed(connection)
        db = SnowflakeGateway(connection)
        db.rows("BEGIN")
        event = Repository(db).event("core-test", 1, "CoreCalculated", {"Death": "250000.00"})
        connection.commit()
        class FailingSink:
            def append(self, event):
                raise ConnectionError("Simulated PostgreSQL outage")
        with pytest.raises(ConnectionError):
            export_pending(db, FailingSink())
        assert db.rows("SELECT COUNT(*) FROM INSURANCE.CALC.AUDIT_OUTBOX WHERE DELIVERED=FALSE")[0][0] == 1
        sink.append(event)  # Simulate delivery followed by a crash before acknowledgement.
        assert export_pending(db, sink) == 1
        assert export_pending(db, sink) == 0
        assert sink.read("core-test", 1, event["eventId"]) == event
        with pytest.raises(ValueError, match="Conflicting"):
            sink.append({**event, "payload": {"Death": "0.00"}})
        assert sink.read("core-test", 1, event["eventId"]) == event


@pytest.mark.postgres
def test_large_event_is_one_row_and_cannot_be_changed(sink):
    event = {"eventId": str(uuid.uuid4()), "aggregateId": "variation-large", "revision": 3,
        "type": "VariationCalculated", "recordedAt": "2026-09-15T00:00:00Z", "payload": {"data": "Ł" * 300000}}
    assert sink.read(event["aggregateId"], 3, event["eventId"]) is None
    sink.append(event)
    assert sink.read(event["aggregateId"], 3, event["eventId"]) == event
    assert sink.read(event["aggregateId"], 4, event["eventId"]) is None
    # Insert-only: the exporter's role has no such privilege (42501) and a trigger stops everyone else (GEA03).
    for statement in ('UPDATE calc."AuditEvent" SET "Revision" = 4 WHERE "EventId" = %s::uuid',
                      'DELETE FROM calc."AuditEvent" WHERE "EventId" = %s::uuid'):
        with pytest.raises(psycopg.Error) as refused:
            sink.connection.execute(statement, (event["eventId"],))
        assert refused.value.sqlstate in {"42501", "GEA03"}
    assert sink.read(event["aggregateId"], 3, event["eventId"]) == event


def test_model_transaction_rollback_removes_audit_and_outbox(emulator):
    with snowflake.connector.connect(**emulator) as connection:
        seed(connection)
        db = SnowflakeGateway(connection)
        db.rows("BEGIN")
        Repository(db).event("failed-model", 1, "CoreCalculated", {"Death": "10.00"})
        connection.rollback()
        assert db.rows("SELECT COUNT(*) FROM INSURANCE.CALC.AUDIT_EVENTS")[0][0] == 0
        assert db.rows("SELECT COUNT(*) FROM INSURANCE.CALC.AUDIT_OUTBOX")[0][0] == 0


@pytest.mark.postgres
def test_frontend_calculation_result_reaches_postgresql(emulator, sink):
    from tests.test_calculation_api import running_calculation_api, call, core_request, fork, calculate
    with snowflake.connector.connect(**emulator) as connection:
        seed(connection)
        with running_calculation_api(emulator) as url:
            core = call(url, "/core-models", "POST", core_request(), status=201)
            variation = calculate(url, fork(url, core))
            expected = call(url, f"/audit/{variation['modelId']}")["events"][-1]
            assert export_pending(SnowflakeGateway(connection), sink) == 3
            stored = sink.read(variation["modelId"], variation["revision"], expected["eventId"])
            assert stored == expected
            assert stored["payload"]["results"] == variation["results"]

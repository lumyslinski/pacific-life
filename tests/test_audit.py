import json
import os
import uuid

import boto3
from botocore.config import Config
import pytest
import snowflake.connector

from calculation_api.audit_export import DynamoAudit, export_pending
from calculation_api.bootstrap import seed
from calculation_api.gateway import SnowflakeGateway
from calculation_api.repository import Repository


@pytest.fixture
def sink():
    endpoint = os.getenv("DYNAMODB_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("Set DYNAMODB_TEST_ENDPOINT to a local DynamoDB or Moto server for AWS adapter tests.")
    from urllib.parse import urlparse
    assert urlparse(endpoint).hostname in {"localhost", "127.0.0.1", "::1"}, "Only loopback endpoints are allowed in tests."
    client = boto3.client("dynamodb", endpoint_url=endpoint, region_name="eu-central-1",
        aws_access_key_id="local", aws_secret_access_key="local",
        config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 2}))
    table = "AuditTest" + uuid.uuid4().hex
    sink = DynamoAudit(client, table)
    sink.create_table()
    yield sink
    client.delete_table(TableName=table)


@pytest.mark.aws_local
def test_outbox_retry_and_duplicate_delivery(emulator, sink):
    with snowflake.connector.connect(**emulator) as connection:
        seed(connection)
        db = SnowflakeGateway(connection)
        db.rows("BEGIN")
        event = Repository(db).event("core-test", 1, "CoreCalculated", {"Death": "250000.00"})
        connection.commit()
        class FailingSink:
            def append(self, event):
                raise ConnectionError("Simulated AWS endpoint outage")
        with pytest.raises(ConnectionError):
            export_pending(db, FailingSink())
        assert db.rows("SELECT COUNT(*) FROM INSURANCE.CALC.AUDIT_OUTBOX WHERE DELIVERED=FALSE")[0][0] == 1
        sink.append(event)  # Simulate delivery followed by a crash before acknowledgement.
        assert export_pending(db, sink) == 1
        assert export_pending(db, sink) == 0
        assert sink.read("core-test", 1, event["eventId"]) == event
        with pytest.raises(ValueError, match="Conflicting"):
            sink.append({**event, "payload": {"Death": "0.00"}})


@pytest.mark.aws_local
def test_large_event_and_partial_delivery_manifest(sink):
    event = {"eventId": uuid.uuid4().hex, "aggregateId": "variation-large", "revision": 3,
        "type": "VariationCalculated", "recordedAt": "2026-09-15T00:00:00Z", "payload": {"data": "Ł" * 300000}}
    real_put = sink.put_once
    attempted = 0
    def fail_after_first_chunk(item):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise ConnectionError("Lost connection partway through export")
        real_put(item)
    sink.put_once = fail_after_first_chunk
    with pytest.raises(ConnectionError):
        sink.append(event)
    assert sink.read(event["aggregateId"], 3, event["eventId"]) is None
    sink.put_once = real_put
    sink.append(event)
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


@pytest.mark.aws_local
def test_frontend_calculation_result_reaches_dynamodb(emulator, sink):
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

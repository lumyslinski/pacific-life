"""Optional AWS audit projection: retryable SQL outbox -> DynamoDB SDK endpoint."""
import argparse
from hashlib import sha256
import json
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import snowflake.connector

from .bootstrap import connection_options
from .gateway import SnowflakeGateway
from .models import canonical_json

CHUNK_BYTES = 256 * 1024  # Below DynamoDB's 400 KB item limit, even with metadata.


def dynamodb_client():
    endpoint = os.getenv("DYNAMODB_ENDPOINT", "http://127.0.0.1:8001")
    options = {"region_name": os.getenv("AWS_REGION", "eu-central-1"),
               "config": Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 2})}
    if endpoint:
        options.update(endpoint_url=endpoint, aws_access_key_id="local", aws_secret_access_key="local")
    # Explicit empty DYNAMODB_ENDPOINT selects AWS with the normal SDK identity chain.
    return boto3.client("dynamodb", **options)


class DynamoAudit:
    def __init__(self, client, table="CalculationAudit"):
        self.client, self.table = client, table

    def create_table(self):
        try:
            self.client.create_table(TableName=self.table, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}])
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceInUseException":
                raise
        self.client.get_waiter("table_exists").wait(TableName=self.table, WaiterConfig={"Delay": 1, "MaxAttempts": 20})

    def put_once(self, item):
        try:
            self.client.put_item(TableName=self.table, Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = self.client.get_item(TableName=self.table, Key={k: item[k] for k in ("PK", "SK")}, ConsistentRead=True).get("Item")
            if existing != item:
                raise ValueError("Conflicting audit content for an existing DynamoDB event key.") from error

    @staticmethod
    def keys(aggregate_id, revision, event_id):
        return "MODEL#" + aggregate_id, f"{revision:020d}#{event_id}"

    def append(self, event):
        payload = canonical_json(event).encode()
        pk, prefix = self.keys(event["aggregateId"], event["revision"], event["eventId"])
        chunks = [payload[i:i + CHUNK_BYTES] for i in range(0, len(payload), CHUNK_BYTES)]
        for index, chunk in enumerate(chunks):
            self.put_once({"PK": {"S": pk}, "SK": {"S": prefix + f"#CHUNK#{index:06d}"},
                           "Data": {"B": chunk}, "Hash": {"S": sha256(chunk).hexdigest()}})
        # A reader treats an event as complete only after this manifest exists.
        self.put_once({"PK": {"S": pk}, "SK": {"S": prefix + "#MANIFEST"},
            "Chunks": {"N": str(len(chunks))}, "Hash": {"S": sha256(payload).hexdigest()},
            "Type": {"S": event["type"]}, "RecordedAt": {"S": event["recordedAt"]}})

    def read(self, aggregate_id, revision, event_id):
        pk, prefix = self.keys(aggregate_id, revision, event_id)
        def item(suffix):
            return self.client.get_item(TableName=self.table,
                Key={"PK": {"S": pk}, "SK": {"S": prefix + suffix}}, ConsistentRead=True).get("Item")
        manifest = item("#MANIFEST")
        if not manifest:
            return None
        data = bytearray()
        for index in range(int(manifest["Chunks"]["N"])):
            chunk = item(f"#CHUNK#{index:06d}")
            if not chunk or sha256(chunk["Data"]["B"]).hexdigest() != chunk["Hash"]["S"]:
                raise ValueError("Missing or corrupt audit chunk.")
            data.extend(chunk["Data"]["B"])
        if sha256(data).hexdigest() != manifest["Hash"]["S"]:
            raise ValueError("Audit event hash mismatch.")
        return json.loads(data)


def export_pending(database, sink, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Export limit must be 1..1000.")
    rows = database.rows("""SELECT E.EVENT_ID,E.DOCUMENT_JSON FROM INSURANCE.CALC.AUDIT_EVENTS E
        JOIN INSURANCE.CALC.AUDIT_OUTBOX O ON E.EVENT_ID=O.EVENT_ID WHERE O.DELIVERED=FALSE
        ORDER BY E.EVENT_ID LIMIT %s""", (limit,))
    sent = 0
    for event_id, payload in rows:
        sink.append(json.loads(payload))
        database.rows("UPDATE INSURANCE.CALC.AUDIT_OUTBOX SET DELIVERED=TRUE WHERE EVENT_ID=%s", (event_id,))
        sent += 1
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--create-table", action="store_true")
    args = parser.parse_args()
    sink = DynamoAudit(dynamodb_client(), os.getenv("AUDIT_TABLE", "CalculationAudit"))
    table_ready = not args.create_table
    while True:
        try:
            if not table_ready:
                sink.create_table()
                table_ready = True
            with snowflake.connector.connect(**connection_options()) as connection:
                sent = export_pending(SnowflakeGateway(connection), sink)
            print(canonical_json({"delivered": sent}), flush=True)
        except Exception as error:
            if args.once:
                raise
            print(canonical_json({"status": "retrying", "error": str(error)}), flush=True)
        if args.once:
            break
        time.sleep(2)


if __name__ == "__main__":
    main()

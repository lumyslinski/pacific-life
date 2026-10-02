"""Audit copy: retryable SQL outbox -> PostgreSQL (calc."AuditEvent").

Snowflake (or the emulator) stays authoritative: the audit event and its outbox
row commit with the model. This process reads undelivered outbox rows and
inserts each event into PostgreSQL once; the event id makes a retry harmless.
The table and the role that may write it come from Alembic revision 0005
(gea/db/postgres).

    AUDIT_DATABASE_URL=postgresql://calc_audit_exporter:secret@localhost:5432/gea \
    python -m calculation_api.audit_export            # poll every 2 seconds; --once for one pass
"""
import argparse
from hashlib import sha256
import json
import os
import time

import psycopg
import snowflake.connector

from .bootstrap import connection_options
from .gateway import SnowflakeGateway
from .models import canonical_json


def postgres_connection(url=None):
    url = url or os.getenv("AUDIT_DATABASE_URL")
    if not url:
        raise SystemExit("Set AUDIT_DATABASE_URL, e.g. postgresql://calc_audit_exporter:secret@localhost:5432/gea")
    # Autocommit: one event is one INSERT, so it is stored completely or not at all.
    return psycopg.connect(url, autocommit=True, connect_timeout=5)


class PostgresAudit:
    def __init__(self, connection):
        self.connection = connection

    def append(self, event):
        document = canonical_json(event)
        content_hash = sha256(document.encode()).hexdigest()
        with self.connection.cursor() as cursor:
            cursor.execute("""INSERT INTO calc."AuditEvent"
                    ("EventId", "AggregateId", "Revision", "EventType", "RecordedAt", "DocumentCanonical", "ContentHash")
                VALUES (%s::uuid, %s::text, %s::integer, %s::text, %s::timestamptz, %s::text, %s::text)
                ON CONFLICT ("EventId") DO NOTHING""",
                (event["eventId"], event["aggregateId"], event["revision"], event["type"],
                 event["recordedAt"], document, content_hash))
            if cursor.rowcount == 1:
                return
            # Already stored: a retry after a crash before the outbox row was acknowledged.
            cursor.execute('SELECT "ContentHash" FROM calc."AuditEvent" WHERE "EventId" = %s::uuid', (event["eventId"],))
            if cursor.fetchone()[0] != content_hash:
                raise ValueError("Conflicting audit content for an existing event id.")

    def read(self, aggregate_id, revision, event_id):
        with self.connection.cursor() as cursor:
            cursor.execute("""SELECT "DocumentCanonical", "ContentHash" FROM calc."AuditEvent"
                WHERE "EventId" = %s::uuid AND "AggregateId" = %s::text AND "Revision" = %s::integer""",
                (event_id, aggregate_id, revision))
            row = cursor.fetchone()
        if row is None:
            return None
        if sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("Audit event hash mismatch.")
        return json.loads(row[0])


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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="export what is pending now, then exit")
    args = parser.parse_args()
    while True:
        try:
            with postgres_connection() as target, snowflake.connector.connect(**connection_options()) as source:
                sent = export_pending(SnowflakeGateway(source), PostgresAudit(target))
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

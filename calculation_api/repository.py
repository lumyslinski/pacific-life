"""SQL persistence. Model snapshots, configuration releases and audit are append-only."""
import json
from datetime import datetime, timezone
import uuid

from .models import canonical_json, fingerprint
from .worker import ModelError


class Conflict(ModelError):
    pass


class NotFound(ModelError):
    pass


class Repository:
    def __init__(self, database):
        self.db = database

    def core(self, model_id):
        rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.CORE_MODELS WHERE MODEL_ID=%s", (model_id,))
        if not rows:
            raise NotFound("Core model not found.")
        return json.loads(rows[0][0])

    def variation(self, model_id, revision=None):
        if revision is None:
            rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.VARIATIONS WHERE MODEL_ID=%s", (model_id,))
        else:
            rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.VARIATION_REVISIONS WHERE MODEL_ID=%s AND REVISION=%s", (model_id, revision))
        if not rows:
            raise NotFound("Variation or revision not found.")
        return json.loads(rows[0][0])

    def save_core(self, doc):
        self.db.rows("INSERT INTO INSURANCE.CALC.CORE_MODELS VALUES (%s,%s)", (doc["modelId"], canonical_json(doc)))

    def save_variation(self, doc, expected_revision=None):
        payload = canonical_json(doc)
        if expected_revision is None:
            self.db.rows("INSERT INTO INSURANCE.CALC.VARIATIONS VALUES (%s,%s,%s)", (doc["modelId"], doc["revision"], payload))
        else:
            rows = self.db.rows("""UPDATE INSURANCE.CALC.VARIATIONS SET REVISION=%s,DOCUMENT_JSON=%s
                WHERE MODEL_ID=%s AND REVISION=%s""", (doc["revision"], payload, doc["modelId"], expected_revision))
            if not rows or rows[0][0] != 1:
                raise Conflict("Variation changed; reload its current revision.")
        self.db.rows("INSERT INTO INSURANCE.CALC.VARIATION_REVISIONS VALUES (%s,%s,%s)", (doc["modelId"], doc["revision"], payload))

    def replay(self, request_id, request):
        rows = self.db.rows("SELECT REQUEST_HASH,DOCUMENT_JSON FROM INSURANCE.CALC.COMMAND_RESULTS WHERE REQUEST_ID=%s", (request_id,))
        if not rows:
            return None
        if rows[0][0] != fingerprint(request):
            raise Conflict("requestId already belongs to another command or payload.")
        return json.loads(rows[0][1])

    def remember(self, request_id, request, response):
        self.db.rows("INSERT INTO INSURANCE.CALC.COMMAND_RESULTS VALUES (%s,%s,%s)", (request_id, fingerprint(request), canonical_json(response)))

    def event(self, aggregate_id, revision, event_type, payload):
        event = {"eventId": str(uuid.uuid4()), "aggregateId": aggregate_id, "revision": revision,
                 "type": event_type, "recordedAt": datetime.now(timezone.utc).isoformat(), "payload": payload}
        event["contentHash"] = fingerprint(event)
        self.db.rows("INSERT INTO INSURANCE.CALC.AUDIT_EVENTS VALUES (%s,%s,%s,%s)",
                     (event["eventId"], aggregate_id, revision, canonical_json(event)))
        # Audit + outbox + model commit atomically in this same SQL connection.
        self.db.rows("INSERT INTO INSURANCE.CALC.AUDIT_OUTBOX VALUES (%s,FALSE)", (event["eventId"],))
        return event

    def audit(self, aggregate_id, after=0, limit=100):
        rows = self.db.rows("""SELECT DOCUMENT_JSON FROM INSURANCE.CALC.AUDIT_EVENTS
            WHERE AGGREGATE_ID=%s AND REVISION>%s ORDER BY REVISION LIMIT %s""", (aggregate_id, after, limit))
        return [json.loads(row[0]) for row in rows]

-- 0005 calculation audit: where the calculation module's audit events are kept
-- outside Snowflake.
--
-- Snowflake stays authoritative: INSURANCE.CALC.AUDIT_EVENTS and its outbox
-- commit with the model. calculation_api.audit_export reads undelivered outbox
-- rows and inserts each event here once; the event id makes a retry harmless.
--
-- The table is in its own schema because it belongs to the calculation module,
-- not to GEA. It shares this migration history because both use one database.
--
--   calc_audit   group role of the exporter: INSERT and SELECT, nothing else.
--
-- Like gea_app and gea_relay it is a NOLOGIN role that the platform gives a
-- login member; it is created here when missing, which needs CREATEROLE.
CREATE SCHEMA calc;

CREATE TABLE calc."AuditEvent" (
    "EventId"            uuid NOT NULL,
    "AggregateId"        text NOT NULL,
    "Revision"           integer NOT NULL,
    "EventType"          text NOT NULL,
    "RecordedAt"         timestamptz NOT NULL,
    -- The event exactly as the exporter read it: canonical JSON text, so the
    -- hash can be recomputed by anyone. "Document" is the same value for queries.
    "DocumentCanonical"  text NOT NULL,
    "Document"           jsonb GENERATED ALWAYS AS ("DocumentCanonical"::jsonb) STORED,
    "ContentHash"        char(64) NOT NULL,
    "ExportedAt"         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_AuditEvent" PRIMARY KEY ("EventId"),
    CONSTRAINT "CK_AuditEvent_AggregateId" CHECK (btrim("AggregateId") <> ''),
    CONSTRAINT "CK_AuditEvent_Revision" CHECK ("Revision" >= 0),
    CONSTRAINT "CK_AuditEvent_EventType" CHECK (btrim("EventType") <> ''),
    CONSTRAINT "CK_AuditEvent_HashMatchesDocument" CHECK (
        "ContentHash" = encode(sha256(convert_to("DocumentCanonical", 'UTF8')), 'hex')),
    CONSTRAINT "CK_AuditEvent_DocumentMatchesColumns" CHECK (
        "DocumentCanonical"::jsonb ->> 'eventId' = "EventId"::text
        AND "DocumentCanonical"::jsonb ->> 'aggregateId' = "AggregateId"
        AND "DocumentCanonical"::jsonb -> 'revision' = to_jsonb("Revision")
        AND "DocumentCanonical"::jsonb ->> 'type' = "EventType")
);

COMMENT ON TABLE calc."AuditEvent" IS
    'Append-only copy of INSURANCE.CALC.AUDIT_EVENTS, written by calculation_api.audit_export. One row per event id.';

-- The audit trail of one model in order.
CREATE INDEX "IX_AuditEvent_Aggregate" ON calc."AuditEvent" ("AggregateId", "Revision", "EventId");

CREATE TRIGGER "AuditEvent_Immutable"
    BEFORE UPDATE OR DELETE ON calc."AuditEvent"
    FOR EACH ROW EXECUTE FUNCTION gea."ForbidMutation"('calc.AuditEvent');

CREATE TRIGGER "AuditEvent_NoTruncate"
    BEFORE TRUNCATE ON calc."AuditEvent"
    FOR EACH STATEMENT EXECUTE FUNCTION gea."ForbidMutation"('calc.AuditEvent');

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'calc_audit') THEN
        BEGIN
            CREATE ROLE calc_audit NOLOGIN;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE EXCEPTION 'role calc_audit does not exist and the migration user may not create roles'
                USING HINT = 'Run CREATE ROLE calc_audit NOLOGIN as an administrator, then upgrade again.';
        END;
    END IF;
END $$;

GRANT USAGE ON SCHEMA calc TO calc_audit;
GRANT SELECT, INSERT ON calc."AuditEvent" TO calc_audit;

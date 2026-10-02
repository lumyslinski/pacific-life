-- 0005 calculation audit, downgrade.
-- Exported events are an audit record and their outbox rows are already marked
-- delivered, so they would not be exported again: the downgrade stops while one
-- exists. The role is cluster-wide and may have members, so it is left in place.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM calc."AuditEvent") THEN
        RAISE EXCEPTION 'calc."AuditEvent" holds exported audit events; refusing to drop them'
            USING HINT = 'Archive the events, then remove them as the table owner before downgrading below 0005.';
    END IF;
END $$;

DROP TABLE calc."AuditEvent";
DROP SCHEMA calc;

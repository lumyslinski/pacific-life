-- 0007 user roles, downgrade.
-- Who was given which role, and by whom, is in the audit log, which is never rewritten:
-- the downgrade stops while such an event exists. The roles themselves go with the table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM gea."AuditEvent" WHERE "AggregateType" = 'User') THEN
        RAISE EXCEPTION 'gea."AuditEvent" holds recorded role changes; refusing to drop them'
            USING HINT = 'Export the audit events of users and remove them as the table owner before downgrading below 0007.';
    END IF;
END $$;

REVOKE ALL ON gea."Role" FROM gea_app;

DROP TRIGGER "Project_BeforeSignOff" ON gea."Project";
DROP FUNCTION gea."Project_BeforeSignOff"();

ALTER TABLE gea."AuditEvent" DROP CONSTRAINT "CK_AuditEvent_AggregateType";
ALTER TABLE gea."AuditEvent" ADD CONSTRAINT "CK_AuditEvent_AggregateType"
    CHECK ("AggregateType" IN ('Project', 'Run', 'DataContract', 'Job'));

DROP INDEX gea."IX_User_RoleId";
ALTER TABLE gea."User"
    DROP CONSTRAINT "FK_User_Role",
    DROP COLUMN "RoleId";

DROP TABLE gea."Role";

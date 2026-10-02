-- 0007 user roles: what a user may do.
--
-- The workbook has no list of roles. It names one ("... and the current user is not a
-- Viewer", sheet 02, Run details) and asks what a collaborator may do (sheet 05,
-- question 5). The roles here were decided on 2 October 2026: Viewer, Preparer,
-- Reviewer, Admin; one role per user, referenced from "User" like the home region.
--
-- A role is a row, and what it allows is three columns. The API and the forms ask
-- "may this user prepare?", never "is this user a Preparer?", so a new role, or a change
-- to what a role allows, is data and not code.
--
--   CanPrepare     create and change projects and runs, submit, cancel, resolve a failure, clone
--   CanReview      sign off (Project.SignedOffBy); later: commit a Basis, sign off an Analysis
--   CanAdminister  give users their role and home region; operations (retry a delivery)
--
-- New SQLSTATE, next to GEA03..GEA05 of revision 0001:
--   GEA06  the user's role does not allow this                 -> 403 forbidden
CREATE TABLE gea."Role" (
    "Id"             text NOT NULL,
    "Name"           text NOT NULL,
    "SortOrder"      smallint NOT NULL,
    "IsActive"       boolean NOT NULL DEFAULT true,
    "CanPrepare"     boolean NOT NULL DEFAULT false,
    "CanReview"      boolean NOT NULL DEFAULT false,
    "CanAdminister"  boolean NOT NULL DEFAULT false,
    CONSTRAINT "PK_Role" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_Role_Name" UNIQUE ("Name"),
    CONSTRAINT "CK_Role_Id" CHECK ("Id" ~ '^[a-z][a-z0-9-]*$')
);

INSERT INTO gea."Role" ("Id", "Name", "SortOrder", "CanPrepare", "CanReview", "CanAdminister") VALUES
    ('viewer',   'Viewer',   1, false, false, false),
    ('preparer', 'Preparer', 2, true,  false, false),
    ('reviewer', 'Reviewer', 3, true,  true,  false),
    ('admin',    'Admin',    4, true,  true,  true);

-- ---------------------------------------------------------------------------
-- User.RoleId. Until now every user could do everything, so the users that exist keep
-- that: they become Admin (the seeded 'system' user never signs in and becomes Viewer).
-- From here on a user seen for the first time is a Viewer unless the identity provider
-- says otherwise, and an Admin raises the role.
-- ---------------------------------------------------------------------------
ALTER TABLE gea."User" ADD COLUMN "RoleId" text;

UPDATE gea."User" SET "RoleId" = CASE WHEN "Subject" = 'system' THEN 'viewer' ELSE 'admin' END;

ALTER TABLE gea."User"
    ALTER COLUMN "RoleId" SET NOT NULL,
    ALTER COLUMN "RoleId" SET DEFAULT 'viewer',
    ADD CONSTRAINT "FK_User_Role" FOREIGN KEY ("RoleId") REFERENCES gea."Role" ("Id");

CREATE INDEX "IX_User_RoleId" ON gea."User" ("RoleId");

-- A change of role is recorded like every other business event.
ALTER TABLE gea."AuditEvent" DROP CONSTRAINT "CK_AuditEvent_AggregateType";
ALTER TABLE gea."AuditEvent" ADD CONSTRAINT "CK_AuditEvent_AggregateType"
    CHECK ("AggregateType" IN ('Project', 'Run', 'DataContract', 'Job', 'User'));

-- ---------------------------------------------------------------------------
-- The one rule about roles that belongs to the data itself: a project is signed off by
-- a user whose role may review. What a role may do through the API is checked by the
-- API (403); this holds whoever writes the row.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."Project_BeforeSignOff"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW."State" = 'signed-off' AND (TG_OP = 'INSERT' OR OLD."State" <> 'signed-off')
       AND NOT EXISTS (SELECT 1 FROM gea."User" u JOIN gea."Role" r ON r."Id" = u."RoleId"
                       WHERE u."Id" = NEW."SignedOffBy" AND r."CanReview") THEN
        RAISE EXCEPTION 'project "%" can only be signed off by a user whose role may review', NEW."Name"
            USING ERRCODE = 'GEA06';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER "Project_BeforeSignOff"
    BEFORE INSERT OR UPDATE ON gea."Project"
    FOR EACH ROW EXECUTE FUNCTION gea."Project_BeforeSignOff"();

GRANT SELECT ON gea."Role" TO gea_app;

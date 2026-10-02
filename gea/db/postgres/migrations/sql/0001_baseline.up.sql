-- 0001 baseline: shared trigger functions, lookups, users, idempotency receipts
-- and the audit log. PostgreSQL 14+ (gen_random_uuid, sha256, generated columns).
--
-- Naming. Tables and columns are named as the workbook names its data
-- ('03. Model Data': Project.BusinessPurpose, Run.Status ...): PascalCase, no
-- underscores. PostgreSQL folds unquoted names to lower case, so every name in
-- this schema is written in double quotes. Constraints are PK_, FK_, UQ_, CK_
-- and indexes IX_, followed by the table name.
--
-- Business-rule errors raised by the triggers use their own SQLSTATE values, so
-- the API maps them to HTTP without parsing messages:
--   GEA03  the row is locked or immutable                      -> 409 locked
--   GEA04  illegal status transition                           -> 409 invalid_state
--   GEA05  contract does not match the saved configuration     -> 412 precondition_failed
-- Standard codes keep their meaning (23503 foreign key, 23505 unique, 23514 check).

CREATE FUNCTION gea."TouchUpdatedAt"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW."UpdatedAt" := now();
    RETURN NEW;
END $$;

-- Attached to insert-only tables. TG_ARGV[0] is the name used in the error.
CREATE FUNCTION gea."ForbidMutation"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable: % is not allowed', TG_ARGV[0], TG_OP
        USING ERRCODE = 'GEA03';
END $$;

-- ---------------------------------------------------------------------------
-- Lookups. The values are the enums of '03. Model Data' (status Reviewed):
-- Project.Region, Project.BusinessPurpose and Project.Benefit. They are tables,
-- not ENUM types, so that a value can be added without DDL. "Id" is the value
-- the API sends and receives; "Name" is what the workbook shows.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."Region" (
    "Id"         text NOT NULL,
    "Name"       text NOT NULL,
    "SortOrder"  smallint NOT NULL,
    "IsActive"   boolean NOT NULL DEFAULT true,
    CONSTRAINT "PK_Region" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_Region_Name" UNIQUE ("Name"),
    CONSTRAINT "CK_Region_Id" CHECK ("Id" ~ '^[a-z][a-z0-9-]*$')
);

INSERT INTO gea."Region" ("Id", "Name", "SortOrder") VALUES
    ('australia', 'Australia', 1),
    ('asia', 'Asia', 2),
    ('europe', 'Europe', 3),
    ('north-america', 'North America', 4),
    ('global', 'Global', 5);

CREATE TABLE gea."BusinessPurpose" (
    "Id"         text NOT NULL,
    "Name"       text NOT NULL,
    "SortOrder"  smallint NOT NULL,
    "IsActive"   boolean NOT NULL DEFAULT true,
    CONSTRAINT "PK_BusinessPurpose" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_BusinessPurpose_Name" UNIQUE ("Name"),
    CONSTRAINT "CK_BusinessPurpose_Id" CHECK ("Id" ~ '^[a-z][a-z0-9-]*$')
);

INSERT INTO gea."BusinessPurpose" ("Id", "Name", "SortOrder") VALUES
    ('rnd', 'R&D', 1),
    ('pricing', 'Pricing', 2);

CREATE TABLE gea."Benefit" (
    "Id"         text NOT NULL,
    "Name"       text NOT NULL,
    "SortOrder"  smallint NOT NULL,
    "IsActive"   boolean NOT NULL DEFAULT true,
    CONSTRAINT "PK_Benefit" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_Benefit_Name" UNIQUE ("Name"),
    CONSTRAINT "CK_Benefit_Id" CHECK ("Id" ~ '^[a-z][a-z0-9-]*$')
);

INSERT INTO gea."Benefit" ("Id", "Name", "SortOrder") VALUES
    ('mortality', 'Mortality', 1),
    ('morbidity', 'Morbidity', 2),
    ('lapse-persistency', 'Lapse / Persistency', 3),
    ('longevity', 'Longevity', 4),
    ('all-mixed', 'All / Mixed', 5);

-- ---------------------------------------------------------------------------
-- User. The workbook has no User entity, only references to one
-- (Project.Owner, Run.SubmittedBy, Project.SignedOffBy). The API resolves the
-- caller from the identity provider subject and creates the row on first sight.
-- "RegionId" is the user's home region: the region offered first when the user
-- creates a project. It grants or restricts nothing.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."User" (
    "Id"         uuid NOT NULL DEFAULT gen_random_uuid(),
    "Subject"    text NOT NULL,                 -- identity provider subject claim
    "Name"       text NOT NULL,
    "Email"      text,
    "RegionId"   text NOT NULL,
    "CreatedAt"  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_User" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_User_Subject" UNIQUE ("Subject"),
    CONSTRAINT "FK_User_Region" FOREIGN KEY ("RegionId") REFERENCES gea."Region" ("Id"),
    CONSTRAINT "CK_User_Name" CHECK (btrim("Name") <> '')
);

CREATE INDEX "IX_User_RegionId" ON gea."User" ("RegionId");

INSERT INTO gea."User" ("Id", "Subject", "Name", "RegionId")
VALUES ('00000000-0000-0000-0000-000000000001', 'system', 'System', 'global');

-- ---------------------------------------------------------------------------
-- CommandReceipt: the stored answer of a creating request, per caller and
-- Idempotency-Key header. A repeated request gets the first answer, status and
-- headers included; the same key with another request is refused by the API.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."CommandReceipt" (
    "IdempotencyKey"   text NOT NULL,
    "Operation"        text NOT NULL,
    "RequestHash"      char(64) NOT NULL,
    "ResponseStatus"   smallint NOT NULL,
    "ResponseHeaders"  jsonb NOT NULL DEFAULT '{}'::jsonb,
    "ResponseBody"     jsonb NOT NULL,
    "CreatedBy"        uuid NOT NULL,
    "CreatedAt"        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_CommandReceipt" PRIMARY KEY ("CreatedBy", "IdempotencyKey"),
    CONSTRAINT "FK_CommandReceipt_User" FOREIGN KEY ("CreatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_CommandReceipt_IdempotencyKey" CHECK (char_length("IdempotencyKey") BETWEEN 1 AND 255),
    CONSTRAINT "CK_CommandReceipt_RequestHash" CHECK ("RequestHash" ~ '^[0-9a-f]{64}$'),
    CONSTRAINT "CK_CommandReceipt_ResponseHeaders" CHECK (jsonb_typeof("ResponseHeaders") = 'object')
);

CREATE INDEX "IX_CommandReceipt_CreatedAt" ON gea."CommandReceipt" ("CreatedAt");

-- ---------------------------------------------------------------------------
-- AuditEvent: append-only business events for projects, runs and contracts.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."AuditEvent" (
    "Id"             bigint GENERATED ALWAYS AS IDENTITY,
    "AggregateType"  text NOT NULL,
    "AggregateId"    uuid NOT NULL,
    "EventType"      text NOT NULL,
    "Payload"        jsonb NOT NULL DEFAULT '{}'::jsonb,
    "ActorId"        uuid NOT NULL,
    "RequestId"      text,                      -- X-Request-Id of the HTTP request
    "OccurredAt"     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_AuditEvent" PRIMARY KEY ("Id"),
    CONSTRAINT "FK_AuditEvent_User" FOREIGN KEY ("ActorId") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_AuditEvent_AggregateType" CHECK ("AggregateType" IN ('Project', 'Run', 'DataContract'))
);

CREATE INDEX "IX_AuditEvent_Aggregate" ON gea."AuditEvent" ("AggregateType", "AggregateId", "Id");

CREATE TRIGGER "AuditEvent_Immutable"
    BEFORE UPDATE OR DELETE ON gea."AuditEvent"
    FOR EACH ROW EXECUTE FUNCTION gea."ForbidMutation"('AuditEvent');

CREATE TRIGGER "AuditEvent_NoTruncate"
    BEFORE TRUNCATE ON gea."AuditEvent"
    FOR EACH STATEMENT EXECUTE FUNCTION gea."ForbidMutation"('AuditEvent');

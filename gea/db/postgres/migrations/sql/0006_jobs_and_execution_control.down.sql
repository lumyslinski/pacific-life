-- 0006 jobs and execution control, downgrade.
-- A job and the events recorded about it are records of what was submitted, so they are not
-- dropped as a side effect: the downgrade stops while one exists. Log lines are copies of
-- what Snowflake holds and go with the table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM gea."Job") THEN
        RAISE EXCEPTION 'gea."Job" holds submitted jobs; refusing to drop them'
            USING HINT = 'Export the jobs, detach their runs and remove them as the table owner before downgrading below 0006.';
    END IF;
END $$;

REVOKE ALL ON gea."RunCancelRequest", gea."JobSummary", gea."RunLog", gea."Job" FROM gea_app, gea_relay;

DROP VIEW gea."JobSummary";
DROP VIEW gea."RunCancelRequest";

-- CREATE OR REPLACE VIEW cannot take columns away: drop and create the views of 0003 again.
DROP VIEW gea."RunSummary";
DROP VIEW gea."ProjectSummary";

CREATE VIEW gea."ProjectSummary" AS
SELECT p."Id", p."Name", p."RegionId", p."BusinessPurposeId", p."Description", p."Period",
       p."PeriodFrom", p."PeriodTo", p."ParentProjectId", p."State", p."Locked",
       p."SignedOffAt", p."Revision", p."CreatedAt", p."UpdatedAt",
       p."OwnerId", o."Name" AS "OwnerName",
       coalesce(b."Benefits", ARRAY[]::text[]) AS "Benefits",
       coalesce(r."TotalRuns", 0) AS "TotalRuns",
       coalesce(r."CompletedRuns", 0) AS "CompletedRuns",
       coalesce(r."ActiveRuns", 0) AS "ActiveRuns",
       coalesce(r."FailedRuns", 0) AS "FailedRuns",
       coalesce(r."DraftRuns", 0) AS "DraftRuns"
FROM gea."Project" p
JOIN gea."User" o ON o."Id" = p."OwnerId"
LEFT JOIN LATERAL (
    SELECT array_agg(pb."BenefitId" ORDER BY b."SortOrder") AS "Benefits"
    FROM gea."ProjectBenefit" pb JOIN gea."Benefit" b ON b."Id" = pb."BenefitId"
    WHERE pb."ProjectId" = p."Id") b ON true
LEFT JOIN LATERAL (
    SELECT count(*)::integer AS "TotalRuns",
           (count(*) FILTER (WHERE "Status" = 'complete'))::integer AS "CompletedRuns",
           (count(*) FILTER (WHERE "Status" IN ('queued', 'running')))::integer AS "ActiveRuns",
           (count(*) FILTER (WHERE "Status" = 'failed'))::integer AS "FailedRuns",
           (count(*) FILTER (WHERE "Status" = 'draft'))::integer AS "DraftRuns"
    FROM gea."Run" WHERE "ProjectId" = p."Id") r ON true;

CREATE VIEW gea."RunSummary" AS
SELECT r."Id", r."ProjectId", p."Name" AS "ProjectName", p."RegionId", r."Name", r."Treaty",
       r."Status", r."Locked", r."CloneSourceId", r."CurrentContractVersion",
       r."SubmittedAt", r."SubmittedBy", r."FailureMessage", r."Revision", r."CreatedAt", r."UpdatedAt",
       k."Id" AS "CurrentContractId", k."ContentHash" AS "CurrentContractHash",
       d."Status" AS "SnowflakeDeliveryStatus"
FROM gea."Run" r
JOIN gea."Project" p ON p."Id" = r."ProjectId"
LEFT JOIN gea."DataContract" k ON k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"
LEFT JOIN gea."ContractDelivery" d ON d."ContractId" = k."Id" AND d."Target" = 'snowflake';

GRANT SELECT ON gea."ProjectSummary", gea."RunSummary" TO gea_app;

-- The delivery claim of 0003.
CREATE OR REPLACE FUNCTION gea."ClaimContractDeliveries"(p_worker text, p_limit integer DEFAULT 10,
                                             p_lease interval DEFAULT interval '2 minutes')
RETURNS TABLE ("ContractId" uuid, "Target" text, "Attempt" integer, "ProjectId" uuid, "RunId" uuid,
               "Version" integer, "ContentHash" char(64), "DocumentCanonical" text)
LANGUAGE sql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
    WITH due AS (
        SELECT d."ContractId", d."Target"
        FROM gea."ContractDelivery" d
        WHERE (d."Status" = 'pending' AND d."NextAttemptAt" <= now())
           OR (d."Status" = 'delivering' AND d."LockedUntil" <= now())
        ORDER BY d."NextAttemptAt"
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ), claimed AS (
        UPDATE gea."ContractDelivery" d
        SET "Status" = 'delivering', "Attempts" = d."Attempts" + 1,
            "LockedBy" = p_worker, "LockedUntil" = now() + p_lease
        FROM due
        WHERE d."ContractId" = due."ContractId" AND d."Target" = due."Target"
        RETURNING d."ContractId", d."Target", d."Attempts"
    )
    SELECT c."ContractId", c."Target", c."Attempts", k."ProjectId", k."RunId", k."Version",
           k."ContentHash", k."DocumentCanonical"
    FROM claimed c JOIN gea."DataContract" k ON k."Id" = c."ContractId"
$$;

DROP FUNCTION gea."FailStaleExecutions"(interval);
DROP FUNCTION gea."RequestRunCancel"(uuid, uuid);
DROP FUNCTION gea."RecordExecutionLog"(uuid, jsonb);
DROP TABLE gea."RunLog";

-- A withdrawn delivery has no status in 0003: it becomes one that gave up, with the reason kept.
UPDATE gea."ContractDelivery" SET "Status" = 'failed' WHERE "Status" = 'cancelled';
ALTER TABLE gea."ContractDelivery" DROP CONSTRAINT "CK_ContractDelivery_Status";
ALTER TABLE gea."ContractDelivery" ADD CONSTRAINT "CK_ContractDelivery_Status"
    CHECK ("Status" IN ('pending', 'delivering', 'delivered', 'failed'));

-- The lifecycle trigger of 0002.
CREATE OR REPLACE FUNCTION gea."Run_BeforeUpdate"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- Everything except these columns is the configuration of the run.
    c_lifecycle CONSTANT text[] := ARRAY['Status', 'Locked', 'CurrentContractVersion', 'SubmittedAt', 'SubmittedBy',
                                         'FailureMessage', 'Revision', 'UpdatedBy', 'UpdatedAt'];
BEGIN
    IF NEW."Id" <> OLD."Id" OR NEW."ProjectId" <> OLD."ProjectId" THEN
        RAISE EXCEPTION 'the identity and the project of a run cannot change'
            USING ERRCODE = 'GEA03';
    END IF;

    IF OLD."Status" <> 'draft' AND (to_jsonb(NEW) - c_lifecycle) IS DISTINCT FROM (to_jsonb(OLD) - c_lifecycle) THEN
        RAISE EXCEPTION 'run "%" is % and its configuration is locked; clone it instead', OLD."Name", OLD."Status"
            USING ERRCODE = 'GEA03';
    END IF;

    IF NEW."Status" <> OLD."Status" AND (OLD."Status", NEW."Status") NOT IN (
           ('draft', 'queued'), ('queued', 'running'), ('queued', 'failed'),
           ('running', 'complete'), ('running', 'failed'), ('failed', 'draft')) THEN
        RAISE EXCEPTION 'run "%" cannot move from % to %', OLD."Name", OLD."Status", NEW."Status"
            USING ERRCODE = 'GEA04';
    END IF;

    -- The pointer moves only when a contract is published (draft -> queued).
    IF NEW."CurrentContractVersion" IS DISTINCT FROM OLD."CurrentContractVersion"
       AND NOT (OLD."Status" = 'draft' AND NEW."Status" = 'queued'
                AND NEW."CurrentContractVersion" > coalesce(OLD."CurrentContractVersion", 0)) THEN
        RAISE EXCEPTION 'the contract pointer of run "%" changes only when a contract is published', OLD."Name"
            USING ERRCODE = 'GEA04';
    END IF;
    IF OLD."Status" = 'draft' AND NEW."Status" = 'queued'
       AND NEW."CurrentContractVersion" IS NOT DISTINCT FROM OLD."CurrentContractVersion" THEN
        RAISE EXCEPTION 'run "%" cannot be queued without a new contract version', OLD."Name"
            USING ERRCODE = 'GEA04';
    END IF;

    NEW."Revision" := OLD."Revision" + 1;
    NEW."UpdatedAt" := now();
    RETURN NEW;
END $$;

DROP INDEX gea."IX_Run_JobId";
ALTER TABLE gea."Run"
    DROP CONSTRAINT "CK_Run_CancelRequest",
    DROP CONSTRAINT "CK_Run_ResolutionState",
    DROP CONSTRAINT "CK_Run_Resolution",
    DROP CONSTRAINT "CK_Run_ResolutionAction",
    DROP CONSTRAINT "CK_Run_Job",
    DROP CONSTRAINT "UQ_Run_JobId_JobOrdinal",
    DROP CONSTRAINT "FK_Run_CancelRequestedBy",
    DROP CONSTRAINT "FK_Run_ResolvedBy",
    DROP CONSTRAINT "FK_Run_Job",
    DROP COLUMN "CancelRequestedBy",
    DROP COLUMN "CancelRequestedAt",
    DROP COLUMN "ResolvedAt",
    DROP COLUMN "ResolvedBy",
    DROP COLUMN "ResolutionNote",
    DROP COLUMN "ResolutionAction",
    DROP COLUMN "JobOrdinal",
    DROP COLUMN "JobId";

ALTER TABLE gea."AuditEvent" DROP CONSTRAINT "CK_AuditEvent_AggregateType";
ALTER TABLE gea."AuditEvent" ADD CONSTRAINT "CK_AuditEvent_AggregateType"
    CHECK ("AggregateType" IN ('Project', 'Run', 'DataContract'));

DROP TABLE gea."Job";

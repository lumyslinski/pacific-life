-- 0006 Jobs, failure resolution, cancellation, the execution log and ordered delivery.
--
-- Who does what when a run executes:
--   Snowflake    executes the steps of a contract and writes one status row per step.
--   PostgreSQL   decides WHEN a contract may start. Delivering a contract to Snowflake is the
--                start signal, and "ClaimContractDeliveries" hands deliveries out in the order
--                and with the parallelism that the run's job allows.
--   The relay    (role gea_relay) carries contracts over, copies status and log lines back,
--                passes cancel requests on and fails executions that went silent.
-- No Python process computes anything or holds a connection for the length of a run.
--
-- Names are the workbook's ('03. Model Data'): Job.*, Run.JobId, Run.ResolutionAction,
-- Run.ResolutionNote, Run.ResolvedBy, Run.ResolvedAt, Run.Logs.

-- ---------------------------------------------------------------------------
-- Job: several runs submitted together. A run submitted on its own has no job.
-- Status, counters, progress and duration are derived from its runs ("JobSummary").
-- ---------------------------------------------------------------------------
CREATE TABLE gea."Job" (
    "Id"           uuid NOT NULL DEFAULT gen_random_uuid(),    -- Job.Id
    "Name"         text NOT NULL,                              -- Job.Name
    "Kind"         text NOT NULL DEFAULT 'data',               -- Job.Kind
    "Note"         text,                                       -- Job.Note
    "ProjectId"    uuid,                                       -- Job.ProjectId; empty when its runs are in several projects
    "MaxParallel"  smallint,     -- how many of its runs may execute at once: 1 = one after another, empty = no limit
    "SubmittedBy"  uuid NOT NULL,                              -- Job.SubmittedBy
    "SubmittedAt"  timestamptz NOT NULL DEFAULT now(),         -- Job.SubmittedAt
    CONSTRAINT "PK_Job" PRIMARY KEY ("Id"),
    CONSTRAINT "FK_Job_Project" FOREIGN KEY ("ProjectId") REFERENCES gea."Project" ("Id"),
    CONSTRAINT "FK_Job_SubmittedBy" FOREIGN KEY ("SubmittedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_Job_Name" CHECK (btrim("Name") <> '' AND char_length("Name") <= 200),
    CONSTRAINT "CK_Job_Kind" CHECK ("Kind" IN ('data', 'basis')),
    CONSTRAINT "CK_Job_Note" CHECK (char_length("Note") <= 2000),
    CONSTRAINT "CK_Job_MaxParallel" CHECK ("MaxParallel" >= 1)
);

CREATE INDEX "IX_Job_ProjectId" ON gea."Job" ("ProjectId", "SubmittedAt" DESC);
CREATE INDEX "IX_Job_SubmittedAt" ON gea."Job" ("SubmittedAt" DESC, "Id");

ALTER TABLE gea."AuditEvent" DROP CONSTRAINT "CK_AuditEvent_AggregateType";
ALTER TABLE gea."AuditEvent" ADD CONSTRAINT "CK_AuditEvent_AggregateType"
    CHECK ("AggregateType" IN ('Project', 'Run', 'DataContract', 'Job'));

-- ---------------------------------------------------------------------------
-- Run: the job it was submitted with, the resolution of a failure and a cancel request.
-- "JobId" and "JobOrdinal" are configuration: they change only while the run is a draft.
-- ---------------------------------------------------------------------------
ALTER TABLE gea."Run"
    ADD COLUMN "JobId"              uuid,          -- Run.JobId: a run may exist without a job
    ADD COLUMN "JobOrdinal"         smallint,      -- position in the job, from 1: the order of execution
    ADD COLUMN "ResolutionAction"   text,          -- Run.ResolutionAction
    ADD COLUMN "ResolutionNote"     text,          -- Run.ResolutionNote
    ADD COLUMN "ResolvedBy"         uuid,          -- Run.ResolvedBy
    ADD COLUMN "ResolvedAt"         timestamptz,   -- Run.ResolvedAt
    ADD COLUMN "CancelRequestedAt"  timestamptz,
    ADD COLUMN "CancelRequestedBy"  uuid,
    ADD CONSTRAINT "FK_Run_Job" FOREIGN KEY ("JobId") REFERENCES gea."Job" ("Id"),
    ADD CONSTRAINT "FK_Run_ResolvedBy" FOREIGN KEY ("ResolvedBy") REFERENCES gea."User" ("Id"),
    ADD CONSTRAINT "FK_Run_CancelRequestedBy" FOREIGN KEY ("CancelRequestedBy") REFERENCES gea."User" ("Id"),
    ADD CONSTRAINT "UQ_Run_JobId_JobOrdinal" UNIQUE ("JobId", "JobOrdinal"),
    ADD CONSTRAINT "CK_Run_Job" CHECK (("JobId" IS NULL) = ("JobOrdinal" IS NULL) AND "JobOrdinal" >= 1),
    ADD CONSTRAINT "CK_Run_ResolutionAction" CHECK ("ResolutionAction" IN ('rerun-with-fixed-config', 'mark-resolved')),
    ADD CONSTRAINT "CK_Run_Resolution" CHECK (
        ("ResolutionAction" IS NULL) = ("ResolvedAt" IS NULL) AND ("ResolutionAction" IS NULL) = ("ResolvedBy" IS NULL)
        AND ("ResolutionNote" IS NULL OR "ResolutionAction" IS NOT NULL) AND char_length("ResolutionNote") <= 2000),
    -- "Mark resolved" leaves the run failed; "re-run with fixed config" returns it to draft.
    ADD CONSTRAINT "CK_Run_ResolutionState" CHECK (
        "ResolutionAction" IS NULL
        OR ("ResolutionAction" = 'mark-resolved' AND "Status" = 'failed')
        OR ("ResolutionAction" = 'rerun-with-fixed-config' AND "Status" = 'draft')),
    ADD CONSTRAINT "CK_Run_CancelRequest" CHECK (("CancelRequestedAt" IS NULL) = ("CancelRequestedBy" IS NULL));

CREATE INDEX "IX_Run_JobId" ON gea."Run" ("JobId", "JobOrdinal") WHERE "JobId" IS NOT NULL;

CREATE OR REPLACE FUNCTION gea."Run_BeforeUpdate"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- Everything except these columns is the configuration of the run (the job it belongs to included).
    c_lifecycle CONSTANT text[] := ARRAY['Status', 'Locked', 'CurrentContractVersion', 'SubmittedAt', 'SubmittedBy',
                                         'FailureMessage', 'ResolutionAction', 'ResolutionNote', 'ResolvedBy', 'ResolvedAt',
                                         'CancelRequestedAt', 'CancelRequestedBy', 'Revision', 'UpdatedBy', 'UpdatedAt'];
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

    -- Only a failure can be resolved, and only an execution that is under way can be cancelled.
    IF NEW."ResolutionAction" IS NOT NULL AND OLD."Status" <> 'failed'
       AND (NEW."ResolutionAction", NEW."ResolvedAt") IS DISTINCT FROM (OLD."ResolutionAction", OLD."ResolvedAt") THEN
        RAISE EXCEPTION 'only a failed run can be resolved; run "%" is %', OLD."Name", OLD."Status"
            USING ERRCODE = 'GEA04';
    END IF;
    IF NEW."CancelRequestedAt" IS NOT NULL AND OLD."CancelRequestedAt" IS NULL
       AND OLD."Status" NOT IN ('queued', 'running') THEN
        RAISE EXCEPTION 'run "%" is % and cannot be cancelled', OLD."Name", OLD."Status"
            USING ERRCODE = 'GEA04';
    END IF;

    -- A cancel request belongs to one execution; a new submission starts without the
    -- previous failure's resolution.
    IF NEW."Status" = 'draft' AND OLD."Status" <> 'draft' THEN
        NEW."CancelRequestedAt" := NULL;
        NEW."CancelRequestedBy" := NULL;
    END IF;
    IF OLD."Status" = 'draft' AND NEW."Status" = 'queued' THEN
        NEW."ResolutionAction" := NULL;
        NEW."ResolutionNote" := NULL;
        NEW."ResolvedBy" := NULL;
        NEW."ResolvedAt" := NULL;
    END IF;

    NEW."Revision" := OLD."Revision" + 1;
    NEW."UpdatedAt" := now();
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- RunLog: the execution log of a run (Run.Logs). Lines come from Snowflake through
-- the relay; "ExternalId" is the id of the line there, so copying twice adds nothing.
-- Lines written here (cancel, lost execution) get an id of their own.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."RunLog" (
    "Id"          bigint GENERATED ALWAYS AS IDENTITY,
    "RunId"       uuid NOT NULL,
    "ContractId"  uuid NOT NULL,
    "ExternalId"  text NOT NULL DEFAULT ('gea:' || gen_random_uuid()::text),
    "StepKey"     text,
    "Level"       text NOT NULL DEFAULT 'info',
    "Message"     text NOT NULL,
    "Detail"      jsonb,
    "OccurredAt"  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_RunLog" PRIMARY KEY ("Id"),
    CONSTRAINT "FK_RunLog_Run" FOREIGN KEY ("RunId") REFERENCES gea."Run" ("Id"),
    CONSTRAINT "FK_RunLog_DataContract" FOREIGN KEY ("ContractId") REFERENCES gea."DataContract" ("Id"),
    CONSTRAINT "UQ_RunLog_ContractId_ExternalId" UNIQUE ("ContractId", "ExternalId"),
    CONSTRAINT "CK_RunLog_Level" CHECK ("Level" IN ('info', 'warning', 'error')),
    CONSTRAINT "CK_RunLog_Message" CHECK (btrim("Message") <> '')
);

CREATE INDEX "IX_RunLog_RunId" ON gea."RunLog" ("RunId", "Id" DESC);

CREATE TRIGGER "RunLog_Immutable"
    BEFORE UPDATE OR DELETE ON gea."RunLog"
    FOR EACH ROW EXECUTE FUNCTION gea."ForbidMutation"('RunLog');

CREATE TRIGGER "RunLog_NoTruncate"
    BEFORE TRUNCATE ON gea."RunLog"
    FOR EACH STATEMENT EXECUTE FUNCTION gea."ForbidMutation"('RunLog');

-- Copy log lines of one contract.
-- p_entries: [{"id": "...", "stepKey": "...", "level": "info|warning|error", "message": "...",
--              "detail": {...}, "occurredAt": "..."}]. Returns the number of new lines.
CREATE FUNCTION gea."RecordExecutionLog"(p_contract_id uuid, p_entries jsonb) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
DECLARE
    v_run_id  uuid;
    v_count   integer;
BEGIN
    SELECT k."RunId" INTO v_run_id FROM gea."DataContract" k WHERE k."Id" = p_contract_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'contract % does not exist', p_contract_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    INSERT INTO gea."RunLog" ("RunId", "ContractId", "ExternalId", "StepKey", "Level", "Message", "Detail", "OccurredAt")
    SELECT v_run_id, p_contract_id, e.id, e."stepKey", coalesce(e.level, 'info'), e.message, e.detail,
           coalesce(e."occurredAt", now())
    FROM jsonb_to_recordset(p_entries) AS e(id text, "stepKey" text, level text, message text, detail jsonb,
                                            "occurredAt" timestamptz)
    ON CONFLICT ("ContractId", "ExternalId") DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;

-- ---------------------------------------------------------------------------
-- Cancel. A contract that was not sent yet is withdrawn and the run fails at once
-- ('cancelled'). One that is on its way or executing gets a request that the relay
-- passes to Snowflake, where the pipeline stops before its next step ('requested').
-- The workbook has no status "cancelled": a cancelled run is a failed run with the
-- reason in "FailureMessage", and it is resolved like any other failure.
-- ---------------------------------------------------------------------------
ALTER TABLE gea."ContractDelivery" DROP CONSTRAINT "CK_ContractDelivery_Status";
ALTER TABLE gea."ContractDelivery" ADD CONSTRAINT "CK_ContractDelivery_Status"
    CHECK ("Status" IN ('pending', 'delivering', 'delivered', 'failed', 'cancelled'));

CREATE FUNCTION gea."RequestRunCancel"(p_run_id uuid, p_user uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
DECLARE
    v_run        gea."Run"%ROWTYPE;
    v_user_name  text;
    v_contract   uuid;
    v_withdrawn  boolean;
BEGIN
    SELECT * INTO v_run FROM gea."Run" r WHERE r."Id" = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', p_run_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_run."Status" NOT IN ('queued', 'running') THEN
        RAISE EXCEPTION 'run "%" is % and cannot be cancelled', v_run."Name", v_run."Status"
            USING ERRCODE = 'GEA04';
    END IF;
    IF v_run."CancelRequestedAt" IS NOT NULL THEN
        RETURN 'requested';                                   -- asked before: nothing more to do
    END IF;
    SELECT u."Name" INTO STRICT v_user_name FROM gea."User" u WHERE u."Id" = p_user;
    SELECT k."Id" INTO STRICT v_contract FROM gea."DataContract" k
    WHERE k."RunId" = p_run_id AND k."Version" = v_run."CurrentContractVersion";

    UPDATE gea."ContractDelivery" d
    SET "Status" = 'cancelled', "LockedBy" = NULL, "LockedUntil" = NULL,
        "LastError" = 'Cancelled by ' || v_user_name || ' before it was sent.'
    WHERE d."ContractId" = v_contract AND d."Target" = 'snowflake' AND d."Status" IN ('pending', 'failed');
    v_withdrawn := FOUND;

    UPDATE gea."Run" r
    SET "CancelRequestedAt" = now(), "CancelRequestedBy" = p_user, "UpdatedBy" = p_user,
        "Status" = CASE WHEN v_withdrawn THEN 'failed' ELSE r."Status" END,
        "FailureMessage" = CASE WHEN v_withdrawn THEN 'Cancelled by ' || v_user_name || ' before execution started.'
                                ELSE r."FailureMessage" END
    WHERE r."Id" = p_run_id;

    INSERT INTO gea."RunLog" ("RunId", "ContractId", "Level", "Message")
    VALUES (p_run_id, v_contract, 'warning',
            CASE WHEN v_withdrawn THEN 'Cancelled by ' || v_user_name || ' before execution started.'
                 ELSE 'Cancel requested by ' || v_user_name || '; the execution stops before its next step.' END);
    RETURN CASE WHEN v_withdrawn THEN 'cancelled' ELSE 'requested' END;
END $$;

-- What the relay passes on to Snowflake.
CREATE VIEW gea."RunCancelRequest" AS
SELECT k."Id" AS "ContractId", r."Id" AS "RunId", r."CancelRequestedAt", u."Name" AS "RequestedByName"
FROM gea."Run" r
JOIN gea."DataContract" k ON k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"
JOIN gea."User" u ON u."Id" = r."CancelRequestedBy"
WHERE r."CancelRequestedAt" IS NOT NULL AND r."Status" IN ('queued', 'running');

-- ---------------------------------------------------------------------------
-- An execution that went silent. "RecordExecutionStatus" stamps "RunExecution"."UpdatedAt"
-- on every report, so the relay's report is the heartbeat: it reports a contract while
-- Snowflake shows its pipeline as executing. The relay calls this after every poll in which
-- Snowflake answered, so an outage of Snowflake or of the relay does not fail anything; a run
-- that Snowflake no longer knows about cannot hang in 'running' for ever.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."FailStaleExecutions"(p_silent_for interval DEFAULT interval '30 minutes') RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
DECLARE
    v_execution  record;
    v_message    text;
    v_count      integer := 0;
BEGIN
    FOR v_execution IN
        SELECT e."ContractId", e."RunId", e."UpdatedAt" FROM gea."RunExecution" e
        WHERE e."Status" IN ('queued', 'running') AND e."UpdatedAt" < now() - p_silent_for
        FOR UPDATE SKIP LOCKED
    LOOP
        v_message := 'No status from Snowflake since '
                     || to_char(v_execution."UpdatedAt" AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC; the execution is presumed lost.';
        INSERT INTO gea."RunLog" ("RunId", "ContractId", "Level", "Message")
        VALUES (v_execution."RunId", v_execution."ContractId", 'error', v_message);
        PERFORM gea."RecordExecutionStatus"(v_execution."ContractId", 'failed', '[]'::jsonb, v_message);
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END $$;

-- ---------------------------------------------------------------------------
-- Claim due deliveries, now in job order.
-- A run of a job with "MaxParallel" = n may start while fewer than n other runs of the
-- job are either ahead of it (lower "JobOrdinal", not finished) or already started.
-- n = 1 is strict sequence; no "MaxParallel", or no job, is no limit. A run whose
-- delivery gave up ('failed') does not hold the others back.
-- The limit is counted, so claims are serialised with an advisory lock.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gea."ClaimContractDeliveries"(p_worker text, p_limit integer DEFAULT 10,
                                                        p_lease interval DEFAULT interval '2 minutes')
RETURNS TABLE ("ContractId" uuid, "Target" text, "Attempt" integer, "ProjectId" uuid, "RunId" uuid,
               "Version" integer, "ContentHash" char(64), "DocumentCanonical" text)
LANGUAGE sql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
    SELECT pg_advisory_xact_lock(hashtext('gea.ClaimContractDeliveries'));
    WITH due AS (
        SELECT d."ContractId", d."Target"
        FROM gea."ContractDelivery" d
        JOIN gea."DataContract" k ON k."Id" = d."ContractId"
        JOIN gea."Run" r ON r."Id" = k."RunId"
        LEFT JOIN gea."Job" j ON j."Id" = r."JobId"
        WHERE ((d."Status" = 'pending' AND d."NextAttemptAt" <= now())
            OR (d."Status" = 'delivering' AND d."LockedUntil" <= now()))
          AND (j."MaxParallel" IS NULL OR j."MaxParallel" > (
                  SELECT count(*)
                  FROM gea."Run" o
                  JOIN gea."DataContract" ok ON ok."RunId" = o."Id" AND ok."Version" = o."CurrentContractVersion"
                  JOIN gea."ContractDelivery" od ON od."ContractId" = ok."Id" AND od."Target" = d."Target"
                  WHERE o."JobId" = r."JobId" AND o."Id" <> r."Id" AND o."Status" IN ('queued', 'running')
                    AND od."Status" IN ('pending', 'delivering', 'delivered')
                    AND (o."JobOrdinal" < r."JobOrdinal" OR od."Status" IN ('delivering', 'delivered'))))
        ORDER BY d."NextAttemptAt", r."JobOrdinal" NULLS LAST
        LIMIT p_limit
        FOR UPDATE OF d SKIP LOCKED
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

-- ---------------------------------------------------------------------------
-- Read models.
-- ---------------------------------------------------------------------------
-- Project.UnresolvedFailedRuns and Project.JobCount ('03. Model Data'), added at the end.
CREATE OR REPLACE VIEW gea."ProjectSummary" AS
SELECT p."Id", p."Name", p."RegionId", p."BusinessPurposeId", p."Description", p."Period",
       p."PeriodFrom", p."PeriodTo", p."ParentProjectId", p."State", p."Locked",
       p."SignedOffAt", p."Revision", p."CreatedAt", p."UpdatedAt",
       p."OwnerId", o."Name" AS "OwnerName",
       coalesce(b."Benefits", ARRAY[]::text[]) AS "Benefits",
       coalesce(r."TotalRuns", 0) AS "TotalRuns",
       coalesce(r."CompletedRuns", 0) AS "CompletedRuns",
       coalesce(r."ActiveRuns", 0) AS "ActiveRuns",
       coalesce(r."FailedRuns", 0) AS "FailedRuns",
       coalesce(r."DraftRuns", 0) AS "DraftRuns",
       coalesce(r."UnresolvedFailedRuns", 0) AS "UnresolvedFailedRuns",
       (SELECT count(*)::integer FROM gea."Job" j WHERE j."ProjectId" = p."Id") AS "JobCount"
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
           (count(*) FILTER (WHERE "Status" = 'draft'))::integer AS "DraftRuns",
           (count(*) FILTER (WHERE "Status" = 'failed' AND "ResolutionAction" IS NULL))::integer AS "UnresolvedFailedRuns"
    FROM gea."Run" WHERE "ProjectId" = p."Id") r ON true;

CREATE OR REPLACE VIEW gea."RunSummary" AS
SELECT r."Id", r."ProjectId", p."Name" AS "ProjectName", p."RegionId", r."Name", r."Treaty",
       r."Status", r."Locked", r."CloneSourceId", r."CurrentContractVersion",
       r."SubmittedAt", r."SubmittedBy", r."FailureMessage", r."Revision", r."CreatedAt", r."UpdatedAt",
       k."Id" AS "CurrentContractId", k."ContentHash" AS "CurrentContractHash",
       d."Status" AS "SnowflakeDeliveryStatus",
       r."JobId", r."JobOrdinal", r."ResolutionAction", r."CancelRequestedAt"
FROM gea."Run" r
JOIN gea."Project" p ON p."Id" = r."ProjectId"
LEFT JOIN gea."DataContract" k ON k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"
LEFT JOIN gea."ContractDelivery" d ON d."ContractId" = k."Id" AND d."Target" = 'snowflake';

-- Job.Status, Job.CompletedRunCount, FailedRunCount, RunningRunCount, ProgressPercentage and
-- Job.Duration, derived from the runs of the job. A run that was returned to draft to be
-- fixed counts as failed until it is submitted again.
CREATE VIEW gea."JobSummary" AS
SELECT j."Id", j."Name", j."Kind", j."Note", j."ProjectId", p."Name" AS "ProjectName", j."MaxParallel",
       j."SubmittedBy", u."Name" AS "SubmittedByName", j."SubmittedAt",
       c."RunCount", c."CompletedRunCount", c."FailedRunCount", c."RunningRunCount", c."QueuedRunCount",
       CASE WHEN c."RunningRunCount" > 0 THEN 'running'
            WHEN c."QueuedRunCount" > 0 THEN CASE WHEN c."QueuedRunCount" = c."RunCount" THEN 'queued' ELSE 'running' END
            WHEN c."FailedRunCount" = 0 THEN 'complete'
            WHEN c."CompletedRunCount" = 0 THEN 'failed'
            ELSE 'completed-with-failures' END AS "Status",
       round(100.0 * c."CompletedRunCount" / nullif(c."RunCount", 0), 1) AS "ProgressPercentage",
       CASE WHEN c."RunningRunCount" + c."QueuedRunCount" = 0 THEN c."LastFinishedAt" END AS "FinishedAt"
FROM gea."Job" j
JOIN gea."User" u ON u."Id" = j."SubmittedBy"
LEFT JOIN gea."Project" p ON p."Id" = j."ProjectId"
CROSS JOIN LATERAL (
    SELECT count(*)::integer AS "RunCount",
           (count(*) FILTER (WHERE r."Status" = 'complete'))::integer AS "CompletedRunCount",
           (count(*) FILTER (WHERE r."Status" IN ('failed', 'draft')))::integer AS "FailedRunCount",
           (count(*) FILTER (WHERE r."Status" = 'running'))::integer AS "RunningRunCount",
           (count(*) FILTER (WHERE r."Status" = 'queued'))::integer AS "QueuedRunCount",
           max(coalesce(e."FinishedAt", r."UpdatedAt")) AS "LastFinishedAt"
    FROM gea."Run" r
    LEFT JOIN gea."DataContract" k ON k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"
    LEFT JOIN gea."RunExecution" e ON e."ContractId" = k."Id"
    WHERE r."JobId" = j."Id") c;

-- ---------------------------------------------------------------------------
-- Privileges for what this revision adds (see 0004 for the roles).
-- ---------------------------------------------------------------------------
REVOKE ALL ON FUNCTION gea."RequestRunCancel"(uuid, uuid),
                       gea."FailStaleExecutions"(interval),
                       gea."RecordExecutionLog"(uuid, jsonb) FROM PUBLIC;

GRANT SELECT ON gea."Job", gea."RunLog", gea."JobSummary" TO gea_app;
GRANT INSERT ON gea."Job" TO gea_app;
GRANT EXECUTE ON FUNCTION gea."RequestRunCancel"(uuid, uuid) TO gea_app;

GRANT SELECT ON gea."RunCancelRequest", gea."Job" TO gea_relay;
GRANT EXECUTE ON FUNCTION gea."FailStaleExecutions"(interval),
                          gea."RecordExecutionLog"(uuid, jsonb) TO gea_relay;

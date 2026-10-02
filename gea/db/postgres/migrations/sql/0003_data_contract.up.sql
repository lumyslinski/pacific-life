-- 0003 The immutable, versioned data contract, its delivery to Snowflake, the
-- execution status that comes back, and the read models of the list endpoints.
--
-- PostgreSQL is the system of record. A contract row and its pending Snowflake
-- delivery are created in the SAME transaction that locks the run, so the two
-- stores can never disagree about whether a contract exists: Snowflake either
-- has the identical document (same contract id and content hash) or the
-- delivery row says why it does not yet.

CREATE TABLE gea."DataContract" (
    "Id"                 uuid NOT NULL,                    -- contractId, also inside the document
    "RunId"              uuid NOT NULL,
    "ProjectId"          uuid NOT NULL,
    "Version"            integer NOT NULL,                 -- per run: 1, 2, 3 ...
    "SpecVersion"        text NOT NULL,                    -- format of the document itself
    -- Canonical JSON text (sorted keys, compact separators, UTF-8) exactly as hashed
    -- and exactly as sent to Snowflake. jsonb alone would not preserve those bytes.
    "DocumentCanonical"  text NOT NULL,
    "Document"           jsonb GENERATED ALWAYS AS ("DocumentCanonical"::jsonb) STORED,
    "ContentHash"        char(64) NOT NULL,
    "CreatedBy"          uuid NOT NULL,
    "CreatedAt"          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_DataContract" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_DataContract_RunId_Version" UNIQUE ("RunId", "Version"),
    CONSTRAINT "FK_DataContract_Run" FOREIGN KEY ("RunId", "ProjectId") REFERENCES gea."Run" ("Id", "ProjectId"),
    CONSTRAINT "FK_DataContract_CreatedBy" FOREIGN KEY ("CreatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_DataContract_Version" CHECK ("Version" >= 1),
    CONSTRAINT "CK_DataContract_HashMatchesDocument" CHECK (
        "ContentHash" = encode(sha256(convert_to("DocumentCanonical", 'UTF8')), 'hex')),
    CONSTRAINT "CK_DataContract_DocumentMatchesColumns" CHECK (
        "DocumentCanonical"::jsonb ->> 'contractId' = "Id"::text
        AND "DocumentCanonical"::jsonb ->> 'runId' = "RunId"::text
        AND "DocumentCanonical"::jsonb ->> 'projectId' = "ProjectId"::text
        AND "DocumentCanonical"::jsonb -> 'version' = to_jsonb("Version")
        AND "DocumentCanonical"::jsonb ->> 'specVersion' = "SpecVersion"
        AND jsonb_typeof("DocumentCanonical"::jsonb -> 'steps') = 'array')
);

CREATE INDEX "IX_DataContract_ProjectId" ON gea."DataContract" ("ProjectId", "CreatedAt" DESC);

ALTER TABLE gea."Run"
    ADD CONSTRAINT "FK_Run_CurrentContract"
    FOREIGN KEY ("Id", "CurrentContractVersion") REFERENCES gea."DataContract" ("RunId", "Version")
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- ContractDelivery: the outbox. One row per contract and target.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."ContractDelivery" (
    "ContractId"     uuid NOT NULL,
    "Target"         text NOT NULL,
    "Status"         text NOT NULL DEFAULT 'pending',
    "Attempts"       integer NOT NULL DEFAULT 0,
    "NextAttemptAt"  timestamptz NOT NULL DEFAULT now(),
    "LockedBy"       text,
    "LockedUntil"    timestamptz,
    "LastError"      text,
    "DeliveredAt"    timestamptz,
    "ExternalRef"    text,          -- e.g. the Snowflake query id of the MERGE
    "CreatedAt"      timestamptz NOT NULL DEFAULT now(),
    "UpdatedAt"      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_ContractDelivery" PRIMARY KEY ("ContractId", "Target"),
    CONSTRAINT "FK_ContractDelivery_DataContract" FOREIGN KEY ("ContractId") REFERENCES gea."DataContract" ("Id"),
    CONSTRAINT "CK_ContractDelivery_Target" CHECK ("Target" IN ('snowflake')),
    CONSTRAINT "CK_ContractDelivery_Status" CHECK ("Status" IN ('pending', 'delivering', 'delivered', 'failed')),
    CONSTRAINT "CK_ContractDelivery_Attempts" CHECK ("Attempts" >= 0),
    CONSTRAINT "CK_ContractDelivery_Delivered" CHECK (("Status" = 'delivered') = ("DeliveredAt" IS NOT NULL)),
    CONSTRAINT "CK_ContractDelivery_Lease" CHECK (
        ("Status" = 'delivering') = ("LockedBy" IS NOT NULL AND "LockedUntil" IS NOT NULL))
);

CREATE INDEX "IX_ContractDelivery_Due" ON gea."ContractDelivery" ("NextAttemptAt")
    WHERE "Status" IN ('pending', 'delivering');

CREATE TRIGGER "ContractDelivery_TouchUpdatedAt"
    BEFORE UPDATE ON gea."ContractDelivery"
    FOR EACH ROW EXECUTE FUNCTION gea."TouchUpdatedAt"();

-- ---------------------------------------------------------------------------
-- Assemble the contract document for a draft run. The API calls this, serialises
-- the result as canonical JSON (sorted keys, compact separators, UTF-8 - the same
-- form as calculation_api.models.canonical_json), hashes that text with SHA-256
-- and inserts both. The shape has one owner and matches
-- gea/spec/data-contract.schema.json.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."BuildContractDocument"(p_run_id uuid, p_contract_id uuid,
                                           p_created_by uuid, p_created_at timestamptz)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'specVersion', '1.0',
        'contractId', p_contract_id,
        'version', coalesce(r."CurrentContractVersion", 0) + 1,
        'projectId', p."Id",
        'runId', r."Id",
        'createdAt', to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
        'createdBy', jsonb_build_object('id', u."Id", 'name', u."Name"),
        'project', jsonb_build_object(
            'name', p."Name", 'region', p."RegionId", 'businessPurpose', p."BusinessPurposeId",
            'benefit', (SELECT coalesce(jsonb_agg(pb."BenefitId" ORDER BY b."SortOrder"), '[]'::jsonb)
                         FROM gea."ProjectBenefit" pb JOIN gea."Benefit" b ON b."Id" = pb."BenefitId"
                         WHERE pb."ProjectId" = p."Id")),
        'run', jsonb_build_object('name', r."Name", 'treaty', r."Treaty"),
        'steps', gea."RunSteps"(r."Id"))
    FROM gea."Run" r
    JOIN gea."Project" p ON p."Id" = r."ProjectId"
    JOIN gea."User" u ON u."Id" = p_created_by
    WHERE r."Id" = p_run_id
$$;

-- ---------------------------------------------------------------------------
-- Publication guard. Inserting a contract is the only way to submit a run.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."DataContract_BeforeInsert"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_run gea."Run"%ROWTYPE;
BEGIN
    -- Exclusive lock: serialises concurrent publications and blocks writes to the
    -- exclusion list (which take FOR SHARE) until this transaction ends.
    SELECT * INTO v_run FROM gea."Run" WHERE "Id" = NEW."RunId" FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', NEW."RunId" USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_run."Status" <> 'draft' THEN
        RAISE EXCEPTION 'run "%" is % and already has a published contract', v_run."Name", v_run."Status"
            USING ERRCODE = 'GEA03';
    END IF;
    IF NEW."Version" <> coalesce(v_run."CurrentContractVersion", 0) + 1 THEN
        RAISE EXCEPTION 'contract version must be % for run "%"',
            coalesce(v_run."CurrentContractVersion", 0) + 1, v_run."Name"
            USING ERRCODE = 'GEA05';
    END IF;

    -- The frozen document must be the saved configuration, nothing else.
    IF NEW."DocumentCanonical"::jsonb -> 'steps' IS DISTINCT FROM gea."RunSteps"(NEW."RunId")
       OR NEW."DocumentCanonical"::jsonb #>> '{run,name}' IS DISTINCT FROM v_run."Name"
       OR NEW."DocumentCanonical"::jsonb #>> '{run,treaty}' IS DISTINCT FROM v_run."Treaty" THEN
        RAISE EXCEPTION 'contract document does not match the saved configuration of run "%"', v_run."Name"
            USING ERRCODE = 'GEA05';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER "DataContract_BeforeInsert"
    BEFORE INSERT ON gea."DataContract"
    FOR EACH ROW EXECUTE FUNCTION gea."DataContract_BeforeInsert"();

-- Publication effects, atomic with the insert: queue the Snowflake delivery and
-- lock the run by moving it to 'queued'. That update is refused by
-- "CK_Run_RequiredWhenSubmitted" while a required prop is empty.
CREATE FUNCTION gea."DataContract_AfterInsert"() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
BEGIN
    INSERT INTO gea."ContractDelivery" ("ContractId", "Target") VALUES (NEW."Id", 'snowflake');

    UPDATE gea."Run"
    SET "Status" = 'queued',
        "CurrentContractVersion" = NEW."Version",
        "SubmittedAt" = NEW."CreatedAt",
        "SubmittedBy" = NEW."CreatedBy",
        "FailureMessage" = NULL,
        "UpdatedBy" = NEW."CreatedBy"
    WHERE "Id" = NEW."RunId";
    RETURN NULL;
END $$;

CREATE TRIGGER "DataContract_AfterInsert"
    AFTER INSERT ON gea."DataContract"
    FOR EACH ROW EXECUTE FUNCTION gea."DataContract_AfterInsert"();

CREATE TRIGGER "DataContract_Immutable"
    BEFORE UPDATE OR DELETE ON gea."DataContract"
    FOR EACH ROW EXECUTE FUNCTION gea."ForbidMutation"('DataContract');

CREATE TRIGGER "DataContract_NoTruncate"
    BEFORE TRUNCATE ON gea."DataContract"
    FOR EACH STATEMENT EXECUTE FUNCTION gea."ForbidMutation"('DataContract');

-- ---------------------------------------------------------------------------
-- Execution of one published contract, as reported by Snowflake (Run.Steps).
-- ---------------------------------------------------------------------------
CREATE TABLE gea."RunExecution" (
    "ContractId"      uuid NOT NULL,
    "RunId"           uuid NOT NULL,
    "Status"          text NOT NULL DEFAULT 'queued',
    "StartedAt"       timestamptz,
    "FinishedAt"      timestamptz,
    "FailureMessage"  text,
    "UpdatedAt"       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_RunExecution" PRIMARY KEY ("ContractId"),
    CONSTRAINT "FK_RunExecution_DataContract" FOREIGN KEY ("ContractId") REFERENCES gea."DataContract" ("Id"),
    CONSTRAINT "FK_RunExecution_Run" FOREIGN KEY ("RunId") REFERENCES gea."Run" ("Id"),
    CONSTRAINT "CK_RunExecution_Status" CHECK ("Status" IN ('queued', 'running', 'complete', 'failed')),
    CONSTRAINT "CK_RunExecution_Finished" CHECK (("Status" IN ('complete', 'failed')) = ("FinishedAt" IS NOT NULL))
);

CREATE INDEX "IX_RunExecution_RunId" ON gea."RunExecution" ("RunId");

CREATE TABLE gea."RunExecutionStep" (
    "ContractId"  uuid NOT NULL,
    "StepKey"     text NOT NULL,
    "Ordinal"     smallint NOT NULL,
    "Status"      text NOT NULL DEFAULT 'pending',
    "StartedAt"   timestamptz,
    "FinishedAt"  timestamptz,
    "Detail"      jsonb,
    CONSTRAINT "PK_RunExecutionStep" PRIMARY KEY ("ContractId", "StepKey"),
    CONSTRAINT "UQ_RunExecutionStep_Ordinal" UNIQUE ("ContractId", "Ordinal"),
    CONSTRAINT "FK_RunExecutionStep_RunExecution" FOREIGN KEY ("ContractId")
        REFERENCES gea."RunExecution" ("ContractId") ON DELETE CASCADE,
    CONSTRAINT "CK_RunExecutionStep_Status" CHECK ("Status" IN
        ('pending', 'running', 'complete', 'failed', 'skipped', 'reused', 'pending-rerun'))
);

-- ---------------------------------------------------------------------------
-- Delivery worker API. The relay calls these functions; it never updates
-- "ContractDelivery" directly.
-- ---------------------------------------------------------------------------

-- Claim due deliveries with a lease. SKIP LOCKED lets several relays run safely;
-- an expired lease ('delivering' past "LockedUntil") is claimable again.
CREATE FUNCTION gea."ClaimContractDeliveries"(p_worker text, p_limit integer DEFAULT 10,
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

-- Mark delivered and open the execution record. Returns false when the lease
-- was lost (another worker owns the row now); the caller must then do nothing.
CREATE FUNCTION gea."CompleteContractDelivery"(p_contract_id uuid, p_target text,
                                              p_worker text, p_external_ref text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
BEGIN
    UPDATE gea."ContractDelivery"
    SET "Status" = 'delivered', "DeliveredAt" = now(), "ExternalRef" = p_external_ref,
        "LockedBy" = NULL, "LockedUntil" = NULL, "LastError" = NULL
    WHERE "ContractId" = p_contract_id AND "Target" = p_target
      AND "Status" = 'delivering' AND "LockedBy" = p_worker;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    INSERT INTO gea."RunExecution" ("ContractId", "RunId")
    SELECT k."Id", k."RunId" FROM gea."DataContract" k WHERE k."Id" = p_contract_id
    ON CONFLICT ("ContractId") DO NOTHING;

    INSERT INTO gea."RunExecutionStep" ("ContractId", "StepKey", "Ordinal")
    SELECT p_contract_id, s ->> 'key', (s ->> 'ordinal')::smallint
    FROM gea."DataContract" k, jsonb_array_elements(k."Document" -> 'steps') s
    WHERE k."Id" = p_contract_id
    ON CONFLICT ("ContractId", "StepKey") DO NOTHING;
    RETURN true;
END $$;

-- Record a failed attempt. Retries with the given backoff until p_max_attempts,
-- then gives up ('failed'). The run stays 'queued': a delivery failure is a
-- transport problem, not a configuration problem, so the remedy is
-- "RetryContractDelivery", not editing the run. Alert on "Status" = 'failed'.
CREATE FUNCTION gea."FailContractDelivery"(p_contract_id uuid, p_target text, p_worker text,
                                          p_error text, p_retry_in interval DEFAULT interval '30 seconds',
                                          p_max_attempts integer DEFAULT 8)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
DECLARE
    v_status text;
BEGIN
    UPDATE gea."ContractDelivery"
    SET "Status" = CASE WHEN "Attempts" >= p_max_attempts THEN 'failed' ELSE 'pending' END,
        "NextAttemptAt" = now() + p_retry_in,
        "LastError" = left(p_error, 2000), "LockedBy" = NULL, "LockedUntil" = NULL
    WHERE "ContractId" = p_contract_id AND "Target" = p_target
      AND "Status" = 'delivering' AND "LockedBy" = p_worker
    RETURNING "Status" INTO v_status;
    RETURN v_status;      -- NULL when the lease was lost
END $$;

-- Manual retry of a delivery that gave up (POST .../deliveries/{target}/retry).
CREATE FUNCTION gea."RetryContractDelivery"(p_contract_id uuid, p_target text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
BEGIN
    UPDATE gea."ContractDelivery"
    SET "Status" = 'pending', "Attempts" = 0, "NextAttemptAt" = now(), "LastError" = NULL
    WHERE "ContractId" = p_contract_id AND "Target" = p_target AND "Status" = 'failed';
    RETURN FOUND;
END $$;

-- ---------------------------------------------------------------------------
-- Apply an execution status reported by Snowflake.
-- p_steps: [{"key": "...", "status": "...", "startedAt": "...", "finishedAt": "...", "detail": {...}}]
-- Idempotent; ignores reports for a contract that is no longer the run's current one.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."RecordExecutionStatus"(p_contract_id uuid, p_status text,
                                           p_steps jsonb DEFAULT '[]'::jsonb,
                                           p_failure_message text DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = gea, pg_temp AS $$
DECLARE
    v_run_id   uuid;
    v_version  integer;
    v_current  text;
BEGIN
    SELECT e."RunId", k."Version", e."Status" INTO v_run_id, v_version, v_current
    FROM gea."RunExecution" e JOIN gea."DataContract" k ON k."Id" = e."ContractId"
    WHERE e."ContractId" = p_contract_id
    FOR UPDATE OF e;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no execution exists for contract %', p_contract_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_current IN ('complete', 'failed') THEN
        RETURN v_current;                       -- terminal: late or duplicate reports are ignored
    END IF;

    UPDATE gea."RunExecutionStep" s
    SET "Status" = r.status,
        "StartedAt" = coalesce(r."startedAt", s."StartedAt"),
        "FinishedAt" = coalesce(r."finishedAt", s."FinishedAt"),
        "Detail" = coalesce(r.detail, s."Detail")
    FROM jsonb_to_recordset(p_steps) AS r(key text, status text, "startedAt" timestamptz,
                                         "finishedAt" timestamptz, detail jsonb)
    WHERE s."ContractId" = p_contract_id AND s."StepKey" = r.key;

    UPDATE gea."RunExecution"
    SET "Status" = p_status,
        "StartedAt" = CASE WHEN p_status <> 'queued' THEN coalesce("StartedAt", now()) END,
        "FinishedAt" = CASE WHEN p_status IN ('complete', 'failed') THEN now() END,
        "FailureMessage" = CASE WHEN p_status = 'failed' THEN p_failure_message END,
        "UpdatedAt" = now()
    WHERE "ContractId" = p_contract_id;

    -- Mirror onto the run, one legal transition at a time (queued -> running -> terminal).
    IF p_status IN ('running', 'complete', 'failed') THEN
        UPDATE gea."Run" SET "Status" = 'running'
        WHERE "Id" = v_run_id AND "Status" = 'queued' AND "CurrentContractVersion" = v_version;
    END IF;
    IF p_status IN ('complete', 'failed') THEN
        UPDATE gea."Run" SET "Status" = p_status,
               "FailureMessage" = CASE WHEN p_status = 'failed' THEN p_failure_message END
        WHERE "Id" = v_run_id AND "Status" = 'running' AND "CurrentContractVersion" = v_version;
    END IF;
    RETURN p_status;
END $$;

-- ---------------------------------------------------------------------------
-- Read models. The counters are the derived fields of '03. Model Data'
-- (Project.TotalRuns, CompletedRuns, ActiveRuns, FailedRuns) and of
-- '04. API Data' (projects[].runs.*).
-- ---------------------------------------------------------------------------
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

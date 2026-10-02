-- Behavioural smoke test of the GEA schema. Runs in one transaction and rolls
-- back, so it needs a database that is at the Alembic head and holds no other contracts (see run.sh).
-- Every block raises on a failed expectation; psql stops at the first error.
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
SET LOCAL search_path = gea, public;

CREATE FUNCTION pg_temp.expect_error(p_sql text, p_like text, p_sqlstate text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE p_sql;
    EXCEPTION WHEN others THEN
        IF SQLERRM NOT LIKE p_like OR (p_sqlstate IS NOT NULL AND SQLSTATE <> p_sqlstate) THEN
            RAISE EXCEPTION 'expected an error like "%" (%), got "%" (%) from: %',
                p_like, coalesce(p_sqlstate, 'any'), SQLERRM, SQLSTATE, p_sql;
        END IF;
        RETURN;
    END;
    RAISE EXCEPTION 'expected an error like "%", but this succeeded: %', p_like, p_sql;
END $$;

-- Publish the next contract version of a run the way the API does. p_tamper lets
-- a test corrupt the document after it was assembled.
CREATE FUNCTION pg_temp.publish(p_run uuid, p_user uuid, p_tamper jsonb DEFAULT '{}'::jsonb)
RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
    v_id   uuid := gen_random_uuid();
    v_doc  jsonb := gea."BuildContractDocument"(p_run, v_id, p_user, now()) || p_tamper;
    v_text text := v_doc::text;
BEGIN
    INSERT INTO gea."DataContract" ("Id", "RunId", "ProjectId", "Version", "SpecVersion",
                                   "DocumentCanonical", "ContentHash", "CreatedBy")
    VALUES (v_id, p_run, (v_doc ->> 'projectId')::uuid, (v_doc ->> 'version')::integer,
            v_doc ->> 'specVersion', v_text, encode(sha256(convert_to(v_text, 'UTF8')), 'hex'), p_user);
    RETURN v_id;
END $$;

-- Fill every required prop of a run (sheet 'POC Data', column F).
CREATE FUNCTION pg_temp.complete(p_run uuid) RETURNS void
LANGUAGE sql AS $$
    UPDATE gea."Run" SET
        "DataScope" = ARRAY['Policy_v1', 'Claims_v1'], "StudyPeriodStart" = '2016-03-31', "StudyPeriodEnd" = '2026-03-31',
        "StudyPeriodTreatyOverride" = false, "Investigation" = 'mortality',
        "ExposureMethod" = 'initial', "InitialExposureMethod" = 'advance-to-next-birthday',
        "PolicyTenureSegmentation" = 'policy-year',
        "PartialClaimTreatment" = 'record-as-partial-claim', "AmountBasis" = 'lives-and-amounts',
        "ClaimBasis" = 'ibnr', "IbnrMethodology" = 'chain-ladder', "DerivationMethod" = 'from-data',
        "IbnrStudyPeriodStart" = '2016-03-31', "IbnrStudyPeriodEnd" = '2026-03-31',
        "DevelopmentFrequency" = 'monthly', "UpliftFrequency" = 'annual', "EventMonthFilter" = false,
        "IbnrRbnsBasis" = 'amounts-and-counts', "TailStartPeriod" = '24-months',
        "Adjustment" = 'ibnr', "UltimateRbnsBasis" = 'amounts-and-counts',
        "OutputFrequency" = ARRAY['annual', 'monthly'], "AdditionalOutputFields" = ARRAY['PolicyYear', 'IssueAge']
    WHERE "Id" = p_run
$$;

-- A complete draft run, ready to submit.
CREATE FUNCTION pg_temp.new_run(p_project uuid, p_user uuid, p_name text) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE v_run uuid;
BEGIN
    INSERT INTO gea."Run" ("ProjectId", "Name", "Treaty", "CreatedBy", "UpdatedBy")
    VALUES (p_project, p_name, 'TRT-100', p_user, p_user) RETURNING "Id" INTO v_run;
    PERFORM pg_temp.complete(v_run);
    RETURN v_run;
END $$;

-- What the relay does for one run once its contract may start: claim, deliver, report.
CREATE FUNCTION pg_temp.contract_of(p_run uuid) RETURNS uuid
LANGUAGE sql AS $$
    SELECT k."Id" FROM gea."DataContract" k JOIN gea."Run" r ON r."Id" = k."RunId" AND r."CurrentContractVersion" = k."Version"
    WHERE r."Id" = p_run
$$;

CREATE TEMP TABLE t (key text PRIMARY KEY, id uuid) ON COMMIT DROP;

\echo 1. lookups and dropdown values from the workbook
DO $$
BEGIN
    ASSERT (SELECT array_agg("Id" ORDER BY "SortOrder") FROM "Region")
           = ARRAY['australia', 'asia', 'europe', 'north-america', 'global'], 'Project.Region';
    ASSERT (SELECT array_agg("Name" ORDER BY "SortOrder") FROM "BusinessPurpose") = ARRAY['R&D', 'Pricing'], 'Project.BusinessPurpose';
    ASSERT (SELECT count(*) FROM "Benefit") = 5, 'Project.Benefit';
    ASSERT (SELECT count(DISTINCT "Parameter") FROM "ParameterOption") = 20, 'example values for twenty run props';
    ASSERT (SELECT array_agg("Value" ORDER BY "SortOrder") FROM "ParameterOption" WHERE "Parameter" = 'dataScope')
           = ARRAY['Policy_v1', 'Claims_v1'], 'dataScope examples';
    -- 30 configuration props: 28 columns plus two date ranges (two columns each) minus the exclusion list (a table)
    ASSERT (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'gea' AND table_name = 'Run'
            AND column_name NOT IN ('Id', 'ProjectId', 'Name', 'Treaty', 'Status', 'Locked', 'CloneSourceId',
                'CurrentContractVersion', 'SubmittedAt', 'SubmittedBy', 'FailureMessage', 'Revision',
                'CreatedBy', 'CreatedAt', 'UpdatedBy', 'UpdatedAt', 'JobId', 'JobOrdinal', 'ResolutionAction',
                'ResolutionNote', 'ResolvedBy', 'ResolvedAt', 'CancelRequestedAt', 'CancelRequestedBy')) = 31,
           'one column per createRun prop';
    PERFORM pg_temp.expect_error($q$INSERT INTO "Region" ("Id", "Name", "SortOrder") VALUES ('North_America', 'x', 9)$q$, '%CK_Region_Id%', '23514');
END $$;

\echo 2. user: every user has a home region
DO $$
DECLARE v_user uuid;
BEGIN
    PERFORM pg_temp.expect_error($q$INSERT INTO "User" ("Subject", "Name") VALUES ('u-0', 'No Region')$q$, '%"RegionId"%', '23502');
    PERFORM pg_temp.expect_error($q$INSERT INTO "User" ("Subject", "Name", "RegionId") VALUES ('u-0', 'Bad Region', 'mars')$q$, '%FK_User_Region%', '23503');
    INSERT INTO "User" ("Subject", "Name", "RegionId") VALUES ('u-1', 'User Example', 'europe') RETURNING "Id" INTO v_user;
    INSERT INTO t VALUES ('user', v_user);
    ASSERT (SELECT r."Name" FROM "User" u JOIN "Region" r ON r."Id" = u."RegionId" WHERE u."Id" = v_user) = 'Europe', 'user references region';
    ASSERT (SELECT "RegionId" FROM "User" WHERE "Subject" = 'system') = 'global', 'the system user is global';
    PERFORM pg_temp.expect_error($q$DELETE FROM "Region" WHERE "Id" = 'europe'$q$, '%FK_User_Region%', '23503');
END $$;

\echo 3. project
DO $$
DECLARE v_user uuid := (SELECT id FROM t WHERE key = 'user'); v_project uuid;
BEGIN
    INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy")
    VALUES ('No benefit', 'europe', 'rnd', v_user, v_user, v_user);
    PERFORM pg_temp.expect_error('SET CONSTRAINTS ALL IMMEDIATE', '%at least one benefit%');
    DELETE FROM "Project" WHERE "Name" = 'No benefit';

    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy")
        VALUES ('Bad region', 'mars', 'rnd', %1$L, %1$L, %1$L)$q$, v_user), '%FK_Project_Region%', '23503');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy", "State")
        VALUES ('Bad state', 'europe', 'rnd', %1$L, %1$L, %1$L, 'archived')$q$, v_user), '%CK_Project_State%', '23514');

    INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "Period", "OwnerId", "CreatedBy", "UpdatedBy")
    VALUES ('Europe Example', 'north-america', 'rnd', '2026', v_user, v_user, v_user) RETURNING "Id" INTO v_project;
    INSERT INTO "ProjectBenefit" VALUES (v_project, 'mortality'), (v_project, 'longevity');
    SET CONSTRAINTS ALL IMMEDIATE;
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO t VALUES ('project', v_project);

    ASSERT (SELECT "State" = 'in-progress' AND NOT "Locked" AND "Revision" = 1 FROM "Project" WHERE "Id" = v_project), 'project defaults';
    ASSERT (SELECT "Benefits" = ARRAY['mortality', 'longevity'] AND "TotalRuns" = 0 AND "OwnerName" = 'User Example'
            FROM "ProjectSummary" WHERE "Id" = v_project), 'project summary';
    UPDATE "Project" SET "Description" = 'Scope' WHERE "Id" = v_project;
    ASSERT (SELECT "Revision" FROM "Project" WHERE "Id" = v_project) = 2, 'project revision increments';
END $$;

\echo 4. run: one column per prop, a draft may be incomplete, required props are needed to submit
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid := (SELECT id FROM t WHERE key = 'project');
    v_run uuid;
BEGIN
    INSERT INTO "Run" ("ProjectId", "Name", "Treaty", "CreatedBy", "UpdatedBy")
    VALUES (v_project, 'Mortality 2016-2026', 'TRT-001', v_user, v_user) RETURNING "Id" INTO v_run;
    INSERT INTO t VALUES ('run', v_run);
    ASSERT (SELECT "Status" = 'draft' AND NOT "Locked" AND "Revision" = 1 FROM "Run" WHERE "Id" = v_run), 'run defaults';
    ASSERT "RunConfiguration"(v_run) = '{}'::jsonb, 'a new draft has no configuration yet';
    ASSERT jsonb_array_length("RunSteps"(v_run)) = 7, 'seven configuration steps';

    -- typed columns: wrong values are refused by the column type or a constraint
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "StudyPeriodStart" = '2016-02-30' WHERE "Id" = %L$q$, v_run), '%date/time field value out of range%', '22008');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "StudyPeriodStart" = '2026-03-31', "StudyPeriodEnd" = '2016-03-31' WHERE "Id" = %L$q$, v_run), '%CK_Run_StudyPeriod%', '23514');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "IbnrStudyPeriodStart" = '2016-03-31' WHERE "Id" = %L$q$, v_run), '%CK_Run_IbnrStudyPeriod%', '23514');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "EventMonthFilter" = 'maybe' WHERE "Id" = %L$q$, v_run), '%invalid input syntax for type boolean%', '22P02');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "Name" = '  ' WHERE "Id" = %L$q$, v_run), '%CK_Run_Name%', '23514');

    -- a partial save is stored
    UPDATE "Run" SET "DataScope" = ARRAY['Policy_v1', 'Claims_v1'], "StudyPeriodTreatyOverride" = true, "UpdatedBy" = v_user WHERE "Id" = v_run;
    ASSERT (SELECT "Revision" FROM "Run" WHERE "Id" = v_run) = 2, 'run revision increments';
    ASSERT "RunConfiguration"(v_run) = '{"dataScope": ["Policy_v1", "Claims_v1"], "studyPeriodTreatyOverride": true}'::jsonb, 'configuration with workbook prop names';
    INSERT INTO "RunStudyPeriodExclusion" ("RunId", "Ordinal", "StartDate", "EndDate") VALUES (v_run, 1, '2019-03-31', '2020-03-31');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "RunStudyPeriodExclusion" VALUES (%L, 2, '2021-01-01', '2020-01-01')$q$, v_run), '%CK_RunStudyPeriodExclusion_Order%', '23514');
    ASSERT "RunConfiguration"(v_run) -> 'studyPeriodExclusions' = '[{"start": "2019-03-31", "end": "2020-03-31"}]'::jsonb, 'exclusions are part of the configuration';

    -- optimistic concurrency as the API uses it: a stale revision updates nothing
    UPDATE "Run" SET "Name" = "Name" WHERE "Id" = v_run AND "Revision" = 1;
    ASSERT NOT FOUND, 'stale revision matches no row';

    -- submitting needs every required prop
    PERFORM pg_temp.expect_error(format('SELECT pg_temp.publish(%L, %L)', v_run, v_user), '%CK_Run_RequiredWhenSubmitted%', '23514');
    PERFORM pg_temp.complete(v_run);
    UPDATE "Run" SET "OutputFrequency" = NULL WHERE "Id" = v_run;
    PERFORM pg_temp.expect_error(format('SELECT pg_temp.publish(%L, %L)', v_run, v_user), '%CK_Run_RequiredWhenSubmitted%', '23514');
    UPDATE "Run" SET "OutputFrequency" = ARRAY[]::text[] WHERE "Id" = v_run;
    PERFORM pg_temp.expect_error(format('SELECT pg_temp.publish(%L, %L)', v_run, v_user), '%CK_Run_RequiredWhenSubmitted%', '23514');
    -- the one condition of the modelling sheet: a treaty override needs its end-date mapping
    UPDATE "Run" SET "OutputFrequency" = ARRAY['annual', 'monthly'], "StudyPeriodTreatyOverride" = true WHERE "Id" = v_run;
    PERFORM pg_temp.expect_error(format('SELECT pg_temp.publish(%L, %L)', v_run, v_user), '%CK_Run_RequiredWhenSubmitted%', '23514');
    UPDATE "Run" SET "StudyPeriodTreatyOverride" = false WHERE "Id" = v_run;
    ASSERT (SELECT "Status" FROM "Run" WHERE "Id" = v_run) = 'draft', 'a refused submit leaves the run a draft';
END $$;

\echo 5. publish contract v1: immutable, run locked, delivery queued
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_run uuid := (SELECT id FROM t WHERE key = 'run');
    v_contract uuid; v_doc jsonb;
BEGIN
    PERFORM pg_temp.expect_error(format($q$SELECT pg_temp.publish(%L, %L, '{"steps": []}')$q$, v_run, v_user), '%does not match the saved configuration%', 'GEA05');
    PERFORM pg_temp.expect_error(format($q$SELECT pg_temp.publish(%L, %L, '{"version": 2}')$q$, v_run, v_user), '%contract version must be 1%', 'GEA05');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "DataContract" ("Id", "RunId", "ProjectId", "Version", "SpecVersion", "DocumentCanonical", "ContentHash", "CreatedBy")
        SELECT c, r."Id", r."ProjectId", 1, '1.0', "BuildContractDocument"(r."Id", c, %2$L, now())::text, repeat('0', 64), %2$L
        FROM "Run" r, gen_random_uuid() c WHERE r."Id" = %1$L$q$, v_run, v_user), '%CK_DataContract_HashMatchesDocument%', '23514');
    PERFORM pg_temp.expect_error(format($q$SELECT pg_temp.publish(%L, %L, '{"contractId": "00000000-0000-4000-8000-000000000000"}')$q$, v_run, v_user), '%CK_DataContract_DocumentMatchesColumns%');

    v_contract := pg_temp.publish(v_run, v_user);
    INSERT INTO t VALUES ('contract1', v_contract);
    SELECT "Document" INTO v_doc FROM "DataContract" WHERE "Id" = v_contract;
    ASSERT v_doc ->> 'runId' = v_run::text AND (v_doc -> 'version')::int = 1
       AND jsonb_array_length(v_doc -> 'steps') = 7
       AND v_doc #>> '{steps,0,key}' = 'dataAndSetUp' AND v_doc #>> '{steps,6,key}' = 'actualExpected'
       AND v_doc #>> '{steps,3,config,ibnrMethodology}' = 'chain-ladder'
       AND v_doc #> '{steps,0,config,studyPeriod}' = '{"start": "2016-03-31", "end": "2026-03-31"}'
       AND v_doc #> '{steps,0,config,studyPeriodExclusions}' = '[{"start": "2019-03-31", "end": "2020-03-31"}]'
       AND v_doc #> '{steps,5,config}' = '{"adjustment": "ibnr", "ultimateRbnsBasis": "amounts-and-counts"}'
       AND v_doc #> '{steps,4,config}' = '{}'
       AND v_doc #>> '{project,region}' = 'north-america'
       AND v_doc #> '{project,benefit}' = '["mortality", "longevity"]', 'contract document content';
    ASSERT (SELECT "Status" = 'queued' AND "Locked" AND "CurrentContractVersion" = 1 AND "SubmittedBy" = v_user FROM "Run" WHERE "Id" = v_run), 'run queued';
    ASSERT (SELECT "Locked" AND "SnowflakeDeliveryStatus" = 'pending' AND "CurrentContractId" = v_contract FROM "RunSummary" WHERE "Id" = v_run), 'summary after publish';

    PERFORM pg_temp.expect_error(format($q$UPDATE "DataContract" SET "Version" = 9 WHERE "Id" = %L$q$, v_contract), '%DataContract is immutable%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM "DataContract" WHERE "Id" = %L$q$, v_contract), '%DataContract is immutable%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "AmountBasis" = 'lives' WHERE "Id" = %L$q$, v_run), '%configuration is locked%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "Name" = 'renamed' WHERE "Id" = %L$q$, v_run), '%configuration is locked%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM "RunStudyPeriodExclusion" WHERE "RunId" = %L$q$, v_run), '%configuration is locked%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "RunStudyPeriodExclusion" VALUES (%L, 2, '2021-01-01', '2021-12-31')$q$, v_run), '%configuration is locked%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "Status" = 'complete' WHERE "Id" = %L$q$, v_run), '%cannot move from queued to complete%', 'GEA04');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM "Run" WHERE "Id" = %L$q$, v_run), '%cannot be deleted%', 'GEA03');
    PERFORM pg_temp.expect_error(format('SELECT pg_temp.publish(%L, %L)', v_run, v_user), '%already has a published contract%', 'GEA03');
END $$;

\echo 6. delivery to Snowflake: lease, retry with backoff, give up, manual retry, deliver
DO $$
DECLARE
    v_run uuid := (SELECT id FROM t WHERE key = 'run');
    v_contract uuid := (SELECT id FROM t WHERE key = 'contract1');
    v_claim record;
BEGIN
    SELECT * INTO v_claim FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claim."ContractId" = v_contract AND v_claim."Attempt" = 1 AND v_claim."RunId" = v_run
       AND v_claim."ContentHash" = encode(sha256(convert_to(v_claim."DocumentCanonical", 'UTF8')), 'hex'), 'claim returns the exact document';
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-b')) = 0, 'a leased delivery is not claimed twice';
    ASSERT NOT "CompleteContractDelivery"(v_contract, 'snowflake', 'relay-b', 'q'), 'only the lease owner completes';

    ASSERT "FailContractDelivery"(v_contract, 'snowflake', 'relay-a', 'warehouse suspended', interval '30 seconds') = 'pending', 'failed attempt is retried';
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-a')) = 0, 'backoff is honoured';
    UPDATE "ContractDelivery" SET "NextAttemptAt" = now() - interval '1 second' WHERE "ContractId" = v_contract;
    PERFORM "ClaimContractDeliveries"('relay-a');
    ASSERT "FailContractDelivery"(v_contract, 'snowflake', 'relay-a', 'still down', interval '1 second', 2) = 'failed', 'gives up at max attempts';
    ASSERT (SELECT "Status" FROM "Run" WHERE "Id" = v_run) = 'queued', 'a transport failure does not fail the run';
    ASSERT "RetryContractDelivery"(v_contract, 'snowflake') AND NOT "RetryContractDelivery"(v_contract, 'snowflake'), 'manual retry only from failed';

    PERFORM "ClaimContractDeliveries"('relay-a');
    UPDATE "ContractDelivery" SET "LockedUntil" = now() - interval '1 second' WHERE "ContractId" = v_contract;   -- relay-a died
    SELECT * INTO v_claim FROM "ClaimContractDeliveries"('relay-b');
    ASSERT v_claim."Attempt" = 2, 'an expired lease is taken over';
    ASSERT "FailContractDelivery"(v_contract, 'snowflake', 'relay-a', 'late') IS NULL, 'the previous owner lost the lease';
    ASSERT "CompleteContractDelivery"(v_contract, 'snowflake', 'relay-b', '01b2c3-query-id'), 'delivered';
    ASSERT (SELECT "Status" = 'delivered' AND "ExternalRef" = '01b2c3-query-id' AND "DeliveredAt" IS NOT NULL
            FROM "ContractDelivery" WHERE "ContractId" = v_contract), 'delivery row';
    ASSERT (SELECT "Status" FROM "RunExecution" WHERE "ContractId" = v_contract) = 'queued'
       AND (SELECT count(*) FROM "RunExecutionStep" WHERE "ContractId" = v_contract AND "Status" = 'pending') = 7, 'execution opened with seven pending steps';
END $$;

\echo 7. execution status from Snowflake, failure, reopen, contract v2
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_run uuid := (SELECT id FROM t WHERE key = 'run');
    v_contract uuid := (SELECT id FROM t WHERE key = 'contract1');
    v_contract2 uuid;
BEGIN
    PERFORM "RecordExecutionStatus"(v_contract, 'running', '[{"key": "dataAndSetUp", "status": "complete"}, {"key": "segmentation", "status": "running"}]');
    ASSERT (SELECT "Status" FROM "Run" WHERE "Id" = v_run) = 'running', 'run running';
    PERFORM "RecordExecutionStatus"(v_contract, 'failed', '[{"key": "segmentation", "status": "failed", "detail": {"error": "no exposure rows"}}]', 'Segmentation failed');
    ASSERT (SELECT "Status" = 'failed' AND "FailureMessage" = 'Segmentation failed' FROM "Run" WHERE "Id" = v_run), 'run failed';
    ASSERT (SELECT "Status" = 'failed' AND "Detail" ->> 'error' = 'no exposure rows' FROM "RunExecutionStep"
            WHERE "ContractId" = v_contract AND "StepKey" = 'segmentation'), 'step status recorded';
    ASSERT "RecordExecutionStatus"(v_contract, 'complete') = 'failed', 'a terminal execution ignores late reports';

    UPDATE "Run" SET "Status" = 'draft', "UpdatedBy" = v_user WHERE "Id" = v_run;              -- reopen
    UPDATE "Run" SET "ExposureExclusion" = ARRAY['none'], "UpdatedBy" = v_user WHERE "Id" = v_run;
    v_contract2 := pg_temp.publish(v_run, v_user);
    ASSERT (SELECT "Version" FROM "DataContract" WHERE "Id" = v_contract2) = 2, 'second contract version';
    ASSERT (SELECT "Status" = 'queued' AND "CurrentContractVersion" = 2 AND "FailureMessage" IS NULL FROM "Run" WHERE "Id" = v_run), 'run requeued on v2';
    ASSERT (SELECT "Document" #>> '{steps,1,config,exposureExclusion,0}' FROM "DataContract" WHERE "Id" = v_contract) IS NULL
       AND (SELECT "Document" #>> '{steps,1,config,exposureExclusion,0}' FROM "DataContract" WHERE "Id" = v_contract2) = 'none', 'v1 is unchanged by v2';

    PERFORM "ClaimContractDeliveries"('relay-a');
    PERFORM "CompleteContractDelivery"(v_contract2, 'snowflake', 'relay-a', 'q2');
    PERFORM "RecordExecutionStatus"(v_contract2, 'complete');
    ASSERT (SELECT "Status" FROM "Run" WHERE "Id" = v_run) = 'complete', 'run complete';
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "Status" = 'draft' WHERE "Id" = %L$q$, v_run), '%cannot move from complete to draft%');
    ASSERT (SELECT "CompletedRuns" = 1 AND "TotalRuns" = 1 FROM "ProjectSummary" WHERE "Id" = (SELECT id FROM t WHERE key = 'project')), 'project counters';
END $$;

\echo 8. draft deletion and locked project
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid := (SELECT id FROM t WHERE key = 'project');
    v_draft uuid;
BEGIN
    INSERT INTO "Run" ("ProjectId", "Name", "Treaty", "CreatedBy", "UpdatedBy")
    VALUES (v_project, 'Throwaway', 'TRT-002', v_user, v_user) RETURNING "Id" INTO v_draft;
    INSERT INTO "RunStudyPeriodExclusion" VALUES (v_draft, 1, '2020-01-01', '2020-12-31');
    DELETE FROM "Run" WHERE "Id" = v_draft;
    ASSERT NOT EXISTS (SELECT 1 FROM "RunStudyPeriodExclusion" WHERE "RunId" = v_draft), 'a draft is deleted with its exclusions';

    UPDATE "User" SET "RoleId" = 'reviewer' WHERE "Id" = v_user;      -- a sign-off needs a role that may review (group 17)
    UPDATE "Project" SET "State" = 'signed-off', "SignedOffAt" = now(), "SignedOffBy" = v_user WHERE "Id" = v_project;
    ASSERT (SELECT "Locked" FROM "Project" WHERE "Id" = v_project), 'signed-off project is locked';
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Run" ("ProjectId", "Name", "Treaty", "CreatedBy", "UpdatedBy")
        VALUES (%L, 'Late', 'TRT-003', %2$L, %2$L)$q$, v_project, v_user), '%signed off and locked%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Project" SET "Name" = 'x' WHERE "Id" = %L$q$, v_project), '%signed off and locked%', 'GEA03');
END $$;

\echo 9. dropdown values can be limited to a region, business purpose, benefit or investigation
DO $$
BEGIN
    INSERT INTO "ParameterOption" ("Parameter", "Value", "Label", "SortOrder", "RegionId", "IsDefault")
    VALUES ('exposureMethod', 'central', 'Central', 2, 'europe', true);
    PERFORM pg_temp.expect_error($q$INSERT INTO "ParameterOption" ("Parameter", "Value", "Label", "RegionId")
        VALUES ('exposureMethod', 'central', 'Central again', 'europe')$q$, '%UQ_ParameterOption_Scope%', '23505');
    PERFORM pg_temp.expect_error($q$INSERT INTO "ParameterOption" ("Parameter", "Value", "Label")
        VALUES ('exposureMethod', 'initial', 'Initial again')$q$, '%UQ_ParameterOption_Scope%', '23505');
    PERFORM pg_temp.expect_error($q$INSERT INTO "ParameterOption" ("Parameter", "Value", "Label", "RegionId")
        VALUES ('exposureMethod', 'both', 'Both', 'mars')$q$, '%FK_ParameterOption_Region%', '23503');
    ASSERT (SELECT array_agg("Value" ORDER BY "SortOrder") FROM "ParameterOption"
            WHERE "Parameter" = 'exposureMethod' AND ("RegionId" IS NULL OR "RegionId" = 'europe')) = ARRAY['initial', 'central'], 'europe sees both';
    ASSERT (SELECT array_agg("Value" ORDER BY "SortOrder") FROM "ParameterOption"
            WHERE "Parameter" = 'exposureMethod' AND ("RegionId" IS NULL OR "RegionId" = 'asia')) = ARRAY['initial'], 'asia sees the unscoped value';
END $$;

\echo 10. least-privilege roles
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid; v_run uuid; v_contract uuid;
BEGIN
    ASSERT (SELECT count(*) FROM pg_roles WHERE rolname IN ('gea_app', 'gea_relay')) = 2, 'revision 0004 provides both roles';
    SET LOCAL ROLE gea_app;
    INSERT INTO gea."Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy")
    VALUES ('App role project', 'europe', 'pricing', v_user, v_user, v_user) RETURNING "Id" INTO v_project;
    INSERT INTO gea."ProjectBenefit" VALUES (v_project, 'mortality');
    INSERT INTO gea."Run" ("ProjectId", "Name", "Treaty", "CreatedBy", "UpdatedBy")
    VALUES (v_project, 'App role run', 'TRT-009', v_user, v_user) RETURNING "Id" INTO v_run;
    PERFORM pg_temp.complete(v_run);
    INSERT INTO gea."RunStudyPeriodExclusion" VALUES (v_run, 1, '2019-03-31', '2020-03-31');
    v_contract := pg_temp.publish(v_run, v_user);
    ASSERT (SELECT "Status" FROM gea."ContractDelivery" WHERE "ContractId" = v_contract) = 'pending', 'app role publishes through the triggers';
    PERFORM pg_temp.expect_error(format($q$UPDATE gea."DataContract" SET "Version" = 2 WHERE "Id" = %L$q$, v_contract), '%permission denied%');
    PERFORM pg_temp.expect_error(format($q$UPDATE gea."ContractDelivery" SET "Status" = 'delivered' WHERE "ContractId" = %L$q$, v_contract), '%permission denied%');
    PERFORM pg_temp.expect_error($q$UPDATE gea."Region" SET "Name" = 'x'$q$, '%permission denied%');
    PERFORM pg_temp.expect_error($q$DELETE FROM gea."AuditEvent"$q$, '%permission denied%');
    PERFORM pg_temp.expect_error($q$SELECT * FROM gea."ClaimContractDeliveries"('x')$q$, '%permission denied%');

    SET LOCAL ROLE gea_relay;
    ASSERT (SELECT count(*) FROM gea."ClaimContractDeliveries"('relay-role')) = 1, 'relay role claims';
    ASSERT gea."CompleteContractDelivery"(v_contract, 'snowflake', 'relay-role', 'q3'), 'relay role completes';
    PERFORM pg_temp.expect_error($q$SELECT count(*) FROM gea."Project"$q$, '%permission denied%');
    PERFORM pg_temp.expect_error(format($q$UPDATE gea."Run" SET "Status" = 'complete' WHERE "Id" = %L$q$, v_run), '%permission denied%');
    RESET ROLE;
END $$;

\echo 11. idempotency receipts: one per caller and key, with the stored headers
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_other uuid;
    v_insert text := $q$INSERT INTO "CommandReceipt" ("IdempotencyKey", "Operation", "RequestHash", "ResponseStatus", "ResponseHeaders", "ResponseBody", "CreatedBy")
                        VALUES (%L, 'createProject', %L, 201, %L, '{}', %L)$q$;
BEGIN
    INSERT INTO "User" ("Subject", "Name", "RegionId") VALUES ('u-2', 'Other User', 'asia') RETURNING "Id" INTO v_other;
    EXECUTE format(v_insert, 'key-1', repeat('a', 64), '{"ETag": "\"abc\"", "Location": "/gea/v1/projects/1"}', v_user);
    EXECUTE format(v_insert, 'key-1', repeat('a', 64), '{}', v_other);      -- the same key of another caller is another receipt
    ASSERT (SELECT "ResponseHeaders" ->> 'Location' FROM "CommandReceipt" WHERE "CreatedBy" = v_user AND "IdempotencyKey" = 'key-1') = '/gea/v1/projects/1', 'headers are stored';
    PERFORM pg_temp.expect_error(format(v_insert, 'key-1', repeat('b', 64), '{}', v_user), '%PK_CommandReceipt%', '23505');
    PERFORM pg_temp.expect_error(format(v_insert, repeat('k', 256), repeat('a', 64), '{}', v_user), '%CK_CommandReceipt_IdempotencyKey%', '23514');
    PERFORM pg_temp.expect_error(format(v_insert, 'key-2', repeat('a', 64), '[]', v_user), '%CK_CommandReceipt_ResponseHeaders%', '23514');
    ASSERT (SELECT count(*) FROM "AlembicVersion") = 1, 'exactly one Alembic head is recorded';
END $$;

\echo 12. calculation audit copy: one row per event id, hash-checked, append-only, exporter role
DO $$
DECLARE
    v_id   uuid := gen_random_uuid();
    v_text text := format('{"aggregateId":"core-1","eventId":"%s","payload":{"Death":"250000.00"},"recordedAt":"2026-10-02T08:00:00+00:00","revision":1,"type":"CoreCalculated"}', v_id);
    v_hash text := encode(sha256(convert_to(v_text, 'UTF8')), 'hex');
    v_insert text := $q$INSERT INTO calc."AuditEvent" ("EventId", "AggregateId", "Revision", "EventType", "RecordedAt", "DocumentCanonical", "ContentHash")
                        VALUES (%L, %L, %s, %L, '2026-10-02T08:00:00+00:00', %L, %L)$q$;
BEGIN
    ASSERT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'calc_audit'), 'revision 0005 provides the exporter role';
    SET LOCAL ROLE calc_audit;
    EXECUTE format(v_insert, v_id, 'core-1', 1, 'CoreCalculated', v_text, v_hash);
    ASSERT (SELECT "Document" -> 'payload' ->> 'Death' FROM calc."AuditEvent" WHERE "EventId" = v_id) = '250000.00', 'the document is queryable';
    -- What the exporter sends on a retry: the same event again changes nothing.
    EXECUTE format(v_insert || ' ON CONFLICT ("EventId") DO NOTHING', v_id, 'core-1', 1, 'CoreCalculated', v_text, v_hash);
    ASSERT (SELECT count(*) FROM calc."AuditEvent" WHERE "EventId" = v_id) = 1, 'a repeated delivery does not add a row';
    PERFORM pg_temp.expect_error(format(v_insert, gen_random_uuid(), 'core-1', 1, 'CoreCalculated', v_text, v_hash), '%CK_AuditEvent_DocumentMatchesColumns%', '23514');
    PERFORM pg_temp.expect_error(format(v_insert, v_id, 'core-1', 1, 'CoreCalculated', v_text, repeat('0', 64)), '%CK_AuditEvent_HashMatchesDocument%', '23514');
    PERFORM pg_temp.expect_error(format($q$UPDATE calc."AuditEvent" SET "Revision" = 2 WHERE "EventId" = %L$q$, v_id), '%permission denied%');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM calc."AuditEvent" WHERE "EventId" = %L$q$, v_id), '%permission denied%');
    PERFORM pg_temp.expect_error($q$SELECT count(*) FROM gea."Project"$q$, '%permission denied%');
    RESET ROLE;
    PERFORM pg_temp.expect_error(format($q$UPDATE calc."AuditEvent" SET "Revision" = 2 WHERE "EventId" = %L$q$, v_id), '%calc.AuditEvent is immutable%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM calc."AuditEvent" WHERE "EventId" = %L$q$, v_id), '%calc.AuditEvent is immutable%', 'GEA03');
    PERFORM pg_temp.expect_error('TRUNCATE calc."AuditEvent"', '%calc.AuditEvent is immutable%', 'GEA03');
    SET LOCAL ROLE gea_app;
    PERFORM pg_temp.expect_error('SELECT count(*) FROM calc."AuditEvent"', '%permission denied%');
    RESET ROLE;
END $$;

\echo 13. jobs: runs start in job order, as many at once as the job allows
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid; v_job uuid; v_parallel uuid;
    v_r1 uuid; v_r2 uuid; v_r3 uuid; v_p1 uuid; v_p2 uuid; v_p3 uuid;
    v_claimed uuid[];
BEGIN
    -- Leave nothing from the earlier groups waiting, so that the claims below are only about the jobs.
    UPDATE "ContractDelivery" SET "Status" = 'failed', "LockedBy" = NULL, "LockedUntil" = NULL WHERE "Status" IN ('pending', 'delivering');

    INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy")
    VALUES ('Job project', 'europe', 'pricing', v_user, v_user, v_user) RETURNING "Id" INTO v_project;
    INSERT INTO "ProjectBenefit" VALUES (v_project, 'mortality');
    INSERT INTO t VALUES ('job-project', v_project);

    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Job" ("Name", "MaxParallel", "SubmittedBy") VALUES ('Bad', 0, %L)$q$, v_user), '%CK_Job_MaxParallel%', '23514');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Job" ("Name", "Kind", "SubmittedBy") VALUES ('Bad', 'other', %L)$q$, v_user), '%CK_Job_Kind%', '23514');

    -- A sequential job: MaxParallel = 1.
    INSERT INTO "Job" ("Name", "ProjectId", "MaxParallel", "SubmittedBy") VALUES ('One after another', v_project, 1, v_user) RETURNING "Id" INTO v_job;
    v_r1 := pg_temp.new_run(v_project, v_user, 'Seq 1');
    v_r2 := pg_temp.new_run(v_project, v_user, 'Seq 2');
    v_r3 := pg_temp.new_run(v_project, v_user, 'Seq 3');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "JobId" = %L WHERE "Id" = %L$q$, v_job, v_r1), '%CK_Run_Job%', '23514');
    UPDATE "Run" SET "JobId" = v_job, "JobOrdinal" = 1 WHERE "Id" = v_r1;
    UPDATE "Run" SET "JobId" = v_job, "JobOrdinal" = 2 WHERE "Id" = v_r2;
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "JobId" = %L, "JobOrdinal" = 2 WHERE "Id" = %L$q$, v_job, v_r3), '%UQ_Run_JobId_JobOrdinal%', '23505');
    UPDATE "Run" SET "JobId" = v_job, "JobOrdinal" = 3 WHERE "Id" = v_r3;
    PERFORM pg_temp.publish(v_r3, v_user);                    -- submitted in any order: the ordinal decides
    PERFORM pg_temp.publish(v_r1, v_user);
    PERFORM pg_temp.publish(v_r2, v_user);
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "JobOrdinal" = 9 WHERE "Id" = %L$q$, v_r1), '%configuration is locked%', 'GEA03');
    ASSERT (SELECT "Status" = 'queued' AND "RunCount" = 3 AND "QueuedRunCount" = 3 AND "ProgressPercentage" = 0 AND "FinishedAt" IS NULL
            FROM "JobSummary" WHERE "Id" = v_job), 'a submitted job is queued';
    ASSERT (SELECT "JobCount" FROM "ProjectSummary" WHERE "Id" = v_project) = 1, 'project job counter';

    SELECT array_agg("RunId") INTO v_claimed FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claimed = ARRAY[v_r1], 'only the first run of a sequential job starts';
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-b')) = 0, 'the second waits while the first is on its way';
    PERFORM "CompleteContractDelivery"(pg_temp.contract_of(v_r1), 'snowflake', 'relay-a', 'q');
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_r1), 'running');
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-a')) = 0, 'and while it executes';
    ASSERT (SELECT "Status" = 'running' AND "RunningRunCount" = 1 FROM "JobSummary" WHERE "Id" = v_job), 'job running';
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_r1), 'complete');
    SELECT array_agg("RunId") INTO v_claimed FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claimed = ARRAY[v_r2], 'the second starts when the first has finished';
    ASSERT (SELECT "Status" = 'running' AND "ProgressPercentage" = 33.3 FROM "JobSummary" WHERE "Id" = v_job), 'job a third done';

    -- The second run cannot be sent for now (backoff): the third must not overtake it.
    PERFORM "FailContractDelivery"(pg_temp.contract_of(v_r2), 'snowflake', 'relay-a', 'warehouse suspended', interval '1 hour');
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-a')) = 0, 'order is kept while the second run waits for a retry';
    -- Its delivery gives up: an operator has to look at it, and the rest of the job goes on.
    UPDATE "ContractDelivery" SET "NextAttemptAt" = now() - interval '1 second' WHERE "ContractId" = pg_temp.contract_of(v_r2);
    PERFORM "ClaimContractDeliveries"('relay-a');
    ASSERT "FailContractDelivery"(pg_temp.contract_of(v_r2), 'snowflake', 'relay-a', 'still down', interval '1 second', 2) = 'failed', 'gave up';
    SELECT array_agg("RunId") INTO v_claimed FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claimed = ARRAY[v_r3], 'a delivery that gave up does not hold the job back';
    PERFORM "CompleteContractDelivery"(pg_temp.contract_of(v_r3), 'snowflake', 'relay-a', 'q');
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_r3), 'failed', '[]', 'IBNR failed');
    ASSERT (SELECT "Status" = 'running' AND "FailedRunCount" = 1 AND "CompletedRunCount" = 1 AND "QueuedRunCount" = 1 FROM "JobSummary" WHERE "Id" = v_job), 'one run still waits';
    INSERT INTO t VALUES ('failed-run', v_r3), ('stuck-run', v_r2), ('job', v_job);

    -- A job that allows two at once.
    INSERT INTO "Job" ("Name", "ProjectId", "MaxParallel", "SubmittedBy") VALUES ('Two at once', v_project, 2, v_user) RETURNING "Id" INTO v_parallel;
    v_p1 := pg_temp.new_run(v_project, v_user, 'Par 1');
    v_p2 := pg_temp.new_run(v_project, v_user, 'Par 2');
    v_p3 := pg_temp.new_run(v_project, v_user, 'Par 3');
    UPDATE "Run" SET "JobId" = v_parallel, "JobOrdinal" = 1 WHERE "Id" = v_p1;
    UPDATE "Run" SET "JobId" = v_parallel, "JobOrdinal" = 2 WHERE "Id" = v_p2;
    UPDATE "Run" SET "JobId" = v_parallel, "JobOrdinal" = 3 WHERE "Id" = v_p3;
    PERFORM pg_temp.publish(v_p1, v_user);
    PERFORM pg_temp.publish(v_p2, v_user);
    PERFORM pg_temp.publish(v_p3, v_user);
    SELECT array_agg("RunId" ORDER BY "RunId" = v_p2) INTO v_claimed FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claimed = ARRAY[v_p1, v_p2], 'two runs of the job start together, the third waits';
    PERFORM "CompleteContractDelivery"(pg_temp.contract_of(v_p1), 'snowflake', 'relay-a', 'q');
    PERFORM "CompleteContractDelivery"(pg_temp.contract_of(v_p2), 'snowflake', 'relay-a', 'q');
    ASSERT (SELECT count(*) FROM "ClaimContractDeliveries"('relay-a')) = 0, 'two are executing';
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_p2), 'complete');
    SELECT array_agg("RunId") INTO v_claimed FROM "ClaimContractDeliveries"('relay-a');
    ASSERT v_claimed = ARRAY[v_p3], 'the third starts when one of the two has finished';
    PERFORM "CompleteContractDelivery"(pg_temp.contract_of(v_p3), 'snowflake', 'relay-a', 'q');
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_p1), 'complete');
    PERFORM "RecordExecutionStatus"(pg_temp.contract_of(v_p3), 'complete');
    ASSERT (SELECT "Status" = 'complete' AND "ProgressPercentage" = 100 AND "FinishedAt" IS NOT NULL FROM "JobSummary" WHERE "Id" = v_parallel), 'job complete';
    INSERT INTO t VALUES ('complete-run', v_p1);
END $$;

\echo 14. a failed run is resolved: marked resolved, or returned to draft and run again
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid := (SELECT id FROM t WHERE key = 'job-project');
    v_job uuid := (SELECT id FROM t WHERE key = 'job');
    v_failed uuid := (SELECT id FROM t WHERE key = 'failed-run');
    v_complete uuid := (SELECT id FROM t WHERE key = 'complete-run');
BEGIN
    ASSERT (SELECT "UnresolvedFailedRuns" FROM "ProjectSummary" WHERE "Id" = v_project) = 1, 'one failure waits for a decision';
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "ResolutionAction" = 'mark-resolved', "ResolvedBy" = %L, "ResolvedAt" = now() WHERE "Id" = %L$q$, v_user, v_complete),
                                 '%only a failed run can be resolved%', 'GEA04');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "ResolutionAction" = 'ignore', "ResolvedBy" = %L, "ResolvedAt" = now() WHERE "Id" = %L$q$, v_user, v_failed), '%CK_Run_ResolutionAction%', '23514');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "ResolutionAction" = 'mark-resolved' WHERE "Id" = %L$q$, v_failed), '%CK_Run_Resolution%', '23514');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "ResolutionAction" = 'rerun-with-fixed-config', "ResolvedBy" = %L, "ResolvedAt" = now() WHERE "Id" = %L$q$, v_user, v_failed), '%CK_Run_ResolutionState%', '23514');

    UPDATE "Run" SET "ResolutionAction" = 'mark-resolved', "ResolutionNote" = 'Known data gap, accepted', "ResolvedBy" = v_user, "ResolvedAt" = now() WHERE "Id" = v_failed;
    ASSERT (SELECT "Status" = 'failed' AND "ResolutionAction" = 'mark-resolved' FROM "RunSummary" WHERE "Id" = v_failed), 'marked resolved, still failed';
    ASSERT (SELECT "UnresolvedFailedRuns" = 0 AND "FailedRuns" = 1 FROM "ProjectSummary" WHERE "Id" = v_project), 'no failure waits any more';

    -- Changed their mind: fix the configuration and run it again.
    UPDATE "Run" SET "Status" = 'draft', "ResolutionAction" = 'rerun-with-fixed-config', "ResolutionNote" = NULL, "ResolvedAt" = now(), "UpdatedBy" = v_user WHERE "Id" = v_failed;
    ASSERT (SELECT "FailedRunCount" FROM "JobSummary" WHERE "Id" = v_job) = 1, 'a run that is being fixed still counts as failed in its job';
    UPDATE "Run" SET "TailStartPeriod" = '36-months' WHERE "Id" = v_failed;
    PERFORM pg_temp.publish(v_failed, v_user);
    ASSERT (SELECT "Status" = 'queued' AND "CurrentContractVersion" = 2 AND "ResolutionAction" IS NULL AND "ResolvedBy" IS NULL AND "ResolvedAt" IS NULL
            FROM "Run" WHERE "Id" = v_failed), 'a new submission starts without the old resolution';
END $$;

\echo 15. cancel: withdrawn before it is sent, requested once it is under way
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid := (SELECT id FROM t WHERE key = 'job-project');
    v_rerun uuid := (SELECT id FROM t WHERE key = 'failed-run');
    v_stuck uuid := (SELECT id FROM t WHERE key = 'stuck-run');
    v_complete uuid := (SELECT id FROM t WHERE key = 'complete-run');
    v_draft uuid; v_contract uuid;
BEGIN
    v_draft := pg_temp.new_run(v_project, v_user, 'Never submitted');
    PERFORM pg_temp.expect_error(format('SELECT gea."RequestRunCancel"(%L, %L)', v_draft, v_user), '%is draft and cannot be cancelled%', 'GEA04');
    PERFORM pg_temp.expect_error(format('SELECT gea."RequestRunCancel"(%L, %L)', v_complete, v_user), '%is complete and cannot be cancelled%', 'GEA04');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Run" SET "CancelRequestedAt" = now(), "CancelRequestedBy" = %L WHERE "Id" = %L$q$, v_user, v_complete), '%cannot be cancelled%', 'GEA04');

    -- Not sent yet (here: its delivery had given up): the contract is withdrawn and the run fails at once.
    v_contract := pg_temp.contract_of(v_stuck);
    ASSERT "RequestRunCancel"(v_stuck, v_user) = 'cancelled', 'withdrawn';
    ASSERT (SELECT "Status" = 'failed' AND "FailureMessage" = 'Cancelled by User Example before execution started.' AND "CancelRequestedBy" = v_user
            FROM "Run" WHERE "Id" = v_stuck), 'a withdrawn run is a failed run with the reason';
    ASSERT (SELECT "Status" FROM "ContractDelivery" WHERE "ContractId" = v_contract) = 'cancelled', 'delivery withdrawn';
    ASSERT NOT "RetryContractDelivery"(v_contract, 'snowflake'), 'a withdrawn delivery is not retried';
    ASSERT (SELECT "Level" = 'warning' AND "Message" LIKE 'Cancelled by User Example%' FROM "RunLog" WHERE "RunId" = v_stuck), 'the cancel is in the run log';

    -- Under way: a request the relay passes on; the run keeps its status until Snowflake reports.
    v_contract := pg_temp.contract_of(v_rerun);
    ASSERT (SELECT array_agg("RunId") FROM "ClaimContractDeliveries"('relay-a')) = ARRAY[v_rerun], 'the rerun starts; the withdrawn run is never claimed';
    ASSERT "RequestRunCancel"(v_rerun, v_user) = 'requested', 'on its way: only a request';
    PERFORM "CompleteContractDelivery"(v_contract, 'snowflake', 'relay-a', 'q');
    PERFORM "RecordExecutionStatus"(v_contract, 'running');
    ASSERT "RequestRunCancel"(v_rerun, v_user) = 'requested', 'asking twice changes nothing';
    ASSERT (SELECT count(*) FROM "RunLog" WHERE "RunId" = v_rerun) = 1, 'one log line for one request';
    ASSERT (SELECT "Status" = 'running' AND "CancelRequestedAt" IS NOT NULL FROM "Run" WHERE "Id" = v_rerun), 'still running';
    ASSERT (SELECT "ContractId" = v_contract AND "RequestedByName" = 'User Example' FROM "RunCancelRequest" WHERE "RunId" = v_rerun), 'the relay sees the request';
    PERFORM "RecordExecutionStatus"(v_contract, 'failed', '[{"key": "dataAndSetUp", "status": "complete"}, {"key": "segmentation", "status": "skipped"}]', 'Cancelled by User Example.');
    ASSERT (SELECT "Status" = 'failed' AND "FailureMessage" = 'Cancelled by User Example.' FROM "Run" WHERE "Id" = v_rerun), 'cancelled in Snowflake';
    ASSERT NOT EXISTS (SELECT 1 FROM "RunCancelRequest" WHERE "RunId" = v_rerun), 'nothing left to pass on';

    -- Returned to draft: the cancel request of the old execution is gone.
    UPDATE "Run" SET "Status" = 'draft', "ResolutionAction" = 'rerun-with-fixed-config', "ResolvedBy" = v_user, "ResolvedAt" = now() WHERE "Id" = v_rerun;
    ASSERT (SELECT "CancelRequestedAt" IS NULL AND "CancelRequestedBy" IS NULL FROM "Run" WHERE "Id" = v_rerun), 'a draft carries no cancel request';
    ASSERT (SELECT "Status" FROM "JobSummary" WHERE "Id" = (SELECT id FROM t WHERE key = 'job')) = 'completed-with-failures', 'job finished with failures';
END $$;

\echo 16. execution log and executions that went silent
DO $$
DECLARE
    v_user uuid := (SELECT id FROM t WHERE key = 'user');
    v_project uuid := (SELECT id FROM t WHERE key = 'job-project');
    v_run uuid; v_contract uuid; v_lines text;
BEGIN
    v_run := pg_temp.new_run(v_project, v_user, 'Goes silent');
    v_contract := pg_temp.publish(v_run, v_user);
    PERFORM "ClaimContractDeliveries"('relay-a');
    PERFORM "CompleteContractDelivery"(v_contract, 'snowflake', 'relay-a', 'q');
    PERFORM "RecordExecutionStatus"(v_contract, 'running', '[{"key": "dataAndSetUp", "status": "running"}]');

    v_lines := '[{"id": "sf-1", "stepKey": "dataAndSetUp", "level": "info", "message": "Read 1,204,331 policy rows", "detail": {"rows": 1204331}, "occurredAt": "2026-10-02T08:00:00Z"},
                 {"id": "sf-2", "stepKey": "dataAndSetUp", "level": "warning", "message": "412 rows without an issue date were dropped"}]';
    ASSERT "RecordExecutionLog"(v_contract, v_lines::jsonb) = 2, 'two lines copied';
    ASSERT "RecordExecutionLog"(v_contract, v_lines::jsonb) = 0, 'copying them again adds nothing';
    ASSERT (SELECT "Detail" ->> 'rows' FROM "RunLog" WHERE "ContractId" = v_contract AND "ExternalId" = 'sf-1') = '1204331', 'line detail';
    PERFORM pg_temp.expect_error(format($q$SELECT gea."RecordExecutionLog"(%L, '[{"id": "sf-3", "level": "debug", "message": "x"}]')$q$, v_contract), '%CK_RunLog_Level%', '23514');
    PERFORM pg_temp.expect_error(format($q$SELECT gea."RecordExecutionLog"(%L, '[]')$q$, gen_random_uuid()), '%does not exist%', '23503');
    PERFORM pg_temp.expect_error(format($q$UPDATE "RunLog" SET "Message" = 'x' WHERE "ContractId" = %L$q$, v_contract), '%RunLog is immutable%', 'GEA03');
    PERFORM pg_temp.expect_error(format($q$DELETE FROM "RunLog" WHERE "ContractId" = %L$q$, v_contract), '%RunLog is immutable%', 'GEA03');

    -- Every report from the relay is a heartbeat. Without one for too long the execution is presumed lost.
    ASSERT "FailStaleExecutions"(interval '1 hour') = 0, 'an execution that reports is left alone';
    UPDATE "RunExecution" SET "UpdatedAt" = now() - interval '2 hours' WHERE "ContractId" = v_contract;
    ASSERT "FailStaleExecutions"(interval '1 hour') = 1, 'a silent execution is failed';
    ASSERT (SELECT "Status" = 'failed' AND "FailureMessage" LIKE 'No status from Snowflake since %presumed lost.' FROM "Run" WHERE "Id" = v_run), 'the run does not hang';
    ASSERT (SELECT count(*) FROM "RunLog" WHERE "RunId" = v_run AND "Level" = 'error') = 1, 'and the log says why';
    ASSERT "FailStaleExecutions"(interval '1 hour') = 0, 'once';

    -- Who may do what.
    SET LOCAL ROLE gea_app;
    ASSERT (SELECT count(*) FROM gea."RunLog" WHERE "RunId" = v_run) = 3, 'the API reads the log';
    PERFORM pg_temp.expect_error(format($q$INSERT INTO gea."RunLog" ("RunId", "ContractId", "Message") VALUES (%L, %L, 'x')$q$, v_run, v_contract), '%permission denied%');
    PERFORM pg_temp.expect_error($q$UPDATE gea."Job" SET "Name" = 'x'$q$, '%permission denied%');
    PERFORM pg_temp.expect_error($q$SELECT gea."FailStaleExecutions"()$q$, '%permission denied%');
    PERFORM pg_temp.expect_error(format($q$SELECT gea."RecordExecutionLog"(%L, '[]')$q$, v_contract), '%permission denied%');
    PERFORM pg_temp.expect_error(format('SELECT gea."RequestRunCancel"(%L, %L)', v_run, v_user), '%is failed and cannot be cancelled%', 'GEA04');
    SET LOCAL ROLE gea_relay;
    ASSERT gea."RecordExecutionLog"(v_contract, '[{"id": "sf-9", "message": "late line"}]') = 1, 'the relay copies log lines';
    ASSERT gea."FailStaleExecutions"() = 0, 'and checks for silent executions';
    ASSERT (SELECT count(*) FROM gea."RunCancelRequest") = 0, 'and reads the cancel requests';
    PERFORM pg_temp.expect_error(format('SELECT gea."RequestRunCancel"(%L, %L)', v_run, v_user), '%permission denied%');
    PERFORM pg_temp.expect_error($q$SELECT count(*) FROM gea."RunLog"$q$, '%permission denied%');
    RESET ROLE;
END $$;

\echo 17. roles: what a user may do, and who may sign off
DO $$
DECLARE
    v_viewer uuid; v_preparer uuid; v_reviewer uuid; v_project uuid;
BEGIN
    ASSERT (SELECT array_agg("Id" ORDER BY "SortOrder") FROM "Role") = ARRAY['viewer', 'preparer', 'reviewer', 'admin'], 'the four roles';
    ASSERT (SELECT array_agg(ARRAY["CanPrepare", "CanReview", "CanAdminister"]::text ORDER BY "SortOrder") FROM "Role")
           = ARRAY['{f,f,f}', '{t,f,f}', '{t,t,f}', '{t,t,t}'], 'what each role allows';
    ASSERT (SELECT "RoleId" FROM "User" WHERE "Subject" = 'system') = 'viewer', 'the system user needs no permission';

    INSERT INTO "User" ("Subject", "Name", "RegionId") VALUES ('u-viewer', 'New User', 'europe') RETURNING "Id" INTO v_viewer;
    ASSERT (SELECT "RoleId" FROM "User" WHERE "Id" = v_viewer) = 'viewer', 'a new user is a viewer';
    INSERT INTO "User" ("Subject", "Name", "RegionId", "RoleId") VALUES ('u-preparer', 'Preparer', 'europe', 'preparer') RETURNING "Id" INTO v_preparer;
    INSERT INTO "User" ("Subject", "Name", "RegionId", "RoleId") VALUES ('u-reviewer', 'Reviewer', 'europe', 'reviewer') RETURNING "Id" INTO v_reviewer;
    PERFORM pg_temp.expect_error($q$INSERT INTO "User" ("Subject", "Name", "RegionId", "RoleId") VALUES ('u-x', 'x', 'europe', 'owner')$q$, '%FK_User_Role%', '23503');
    PERFORM pg_temp.expect_error(format($q$UPDATE "User" SET "RoleId" = NULL WHERE "Id" = %L$q$, v_viewer), '%"RoleId"%', '23502');
    PERFORM pg_temp.expect_error($q$DELETE FROM "Role" WHERE "Id" = 'preparer'$q$, '%FK_User_Role%', '23503');
    PERFORM pg_temp.expect_error($q$INSERT INTO "Role" ("Id", "Name", "SortOrder") VALUES ('Super_User', 'x', 9)$q$, '%CK_Role_Id%', '23514');

    -- A project is signed off by a user whose role may review, whoever writes the row.
    INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy")
    VALUES ('To sign off', 'europe', 'pricing', v_preparer, v_preparer, v_preparer) RETURNING "Id" INTO v_project;
    INSERT INTO "ProjectBenefit" VALUES (v_project, 'mortality');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Project" SET "State" = 'signed-off', "SignedOffAt" = now(), "SignedOffBy" = %L WHERE "Id" = %L$q$,
        v_preparer, v_project), '%can only be signed off by a user whose role may review%', 'GEA06');
    PERFORM pg_temp.expect_error(format($q$UPDATE "Project" SET "State" = 'signed-off', "SignedOffAt" = now(), "SignedOffBy" = %L WHERE "Id" = %L$q$,
        v_viewer, v_project), '%can only be signed off by a user whose role may review%', 'GEA06');
    PERFORM pg_temp.expect_error(format($q$INSERT INTO "Project" ("Name", "RegionId", "BusinessPurposeId", "OwnerId", "CreatedBy", "UpdatedBy", "State", "SignedOffAt", "SignedOffBy")
        VALUES ('Born signed off', 'europe', 'pricing', %1$L, %1$L, %1$L, 'signed-off', now(), %1$L)$q$, v_preparer), '%whose role may review%', 'GEA06');
    UPDATE "Project" SET "State" = 'pending-review' WHERE "Id" = v_project;        -- any other state needs no role
    UPDATE "Project" SET "State" = 'signed-off', "SignedOffAt" = now(), "SignedOffBy" = v_reviewer WHERE "Id" = v_project;
    ASSERT (SELECT "Locked" FROM "Project" WHERE "Id" = v_project), 'signed off by the reviewer, and locked';

    -- A new role, or a change to what a role allows, is data.
    INSERT INTO "Role" ("Id", "Name", "SortOrder", "CanPrepare") VALUES ('collaborator', 'Collaborator', 5, true);
    UPDATE "User" SET "RoleId" = 'collaborator' WHERE "Id" = v_viewer;
    ASSERT (SELECT r."CanPrepare" AND NOT r."CanReview" FROM "User" u JOIN "Role" r ON r."Id" = u."RoleId" WHERE u."Id" = v_viewer), 'a role added as a row';

    -- The API login reads the roles and sets a user's role; it cannot change what a role allows.
    SET LOCAL ROLE gea_app;
    ASSERT (SELECT count(*) FROM gea."Role") = 5, 'the API reads the roles';
    UPDATE gea."User" SET "RoleId" = 'preparer' WHERE "Id" = v_viewer;
    INSERT INTO gea."AuditEvent" ("AggregateType", "AggregateId", "EventType", "ActorId") VALUES ('User', v_viewer, 'user.role_changed', v_reviewer);
    PERFORM pg_temp.expect_error($q$UPDATE gea."Role" SET "CanAdminister" = true WHERE "Id" = 'viewer'$q$, '%permission denied%');
    PERFORM pg_temp.expect_error($q$INSERT INTO gea."Role" ("Id", "Name", "SortOrder") VALUES ('root', 'Root', 9)$q$, '%permission denied%');
    SET LOCAL ROLE gea_relay;
    PERFORM pg_temp.expect_error($q$SELECT count(*) FROM gea."Role"$q$, '%permission denied%');
    RESET ROLE;
END $$;

ROLLBACK;
\echo OK: all schema expectations hold

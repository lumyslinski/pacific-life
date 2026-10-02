-- Snowflake executes the run: one task graph run per data contract.
-- STATUS: draft, NOT executed. The local emulator in this repository cannot run
-- procedures or tasks, so nothing below has been run anywhere. Review it with the
-- Snowflake owner; "To validate on a real account" at the end lists what to check first.
--
-- Who does what:
--   Step procedures (GEA.STEP.*)   the calculations. One per workbook step, set-based SQL next to
--                                  the data, or Snowpark Python where SQL cannot express it.
--   RUN_STEP                       runs one step: honours a cancel request, records status before
--                                  and after, turns an error into a failed step.
--   The task graph RUN_PIPELINE    the order of the steps. One graph run per contract; several
--                                  contracts run side by side (OVERLAP_POLICY = ALLOW_ALL_OVERLAP).
--   PostgreSQL + the relay         decide when a contract starts and copy status back. The relay
--                                  starts a run with one statement and holds no connection for it:
--
--       EXECUTE TASK GEA.CONTROL.RUN_PIPELINE USING CONFIG = '{"contractId": "<uuid>"}';
--
-- Nothing in Python computes, waits for, or sequences a step.

CREATE SCHEMA IF NOT EXISTS GEA.STEP;      -- the step procedures
CREATE SCHEMA IF NOT EXISTS GEA.RESULT;    -- what the steps produce, every row keyed by CONTRACT_ID

-- ---------------------------------------------------------------------------
-- Which procedure executes a step of the contract. A step key of the contract
-- (DATA_CONTRACT_STEP.STEP_KEY) is looked up here, never built into a name.
-- ENGINE is information for people: both kinds are called the same way.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS GEA.CONTROL.PIPELINE_STEP (
    STEP_KEY        VARCHAR NOT NULL,
    ORDINAL         INTEGER NOT NULL,
    ENGINE          VARCHAR NOT NULL,      -- sql | snowpark-python
    PROCEDURE_NAME  VARCHAR NOT NULL,      -- fully qualified; signature (CONTRACT_ID VARCHAR) RETURNS VARIANT
    CONSTRAINT PIPELINE_STEP_PK PRIMARY KEY (STEP_KEY)
);

MERGE INTO GEA.CONTROL.PIPELINE_STEP t
USING (SELECT * FROM VALUES
        ('dataAndSetUp',        1, 'sql', 'GEA.STEP.DATA_AND_SET_UP'),
        ('segmentation',        2, 'sql', 'GEA.STEP.SEGMENTATION'),
        ('actuals',             3, 'sql', 'GEA.STEP.ACTUALS'),
        ('ibnr',                4, 'sql', 'GEA.STEP.IBNR'),
        ('assigningExpected',   5, 'sql', 'GEA.STEP.ASSIGNING_EXPECTED'),
        ('ultimateCalculation', 6, 'sql', 'GEA.STEP.ULTIMATE_CALCULATION'),
        ('actualExpected',      7, 'sql', 'GEA.STEP.ACTUAL_EXPECTED')
       AS v (STEP_KEY, ORDINAL, ENGINE, PROCEDURE_NAME)) s
   ON t.STEP_KEY = s.STEP_KEY
 WHEN NOT MATCHED THEN
      INSERT (STEP_KEY, ORDINAL, ENGINE, PROCEDURE_NAME) VALUES (s.STEP_KEY, s.ORDINAL, s.ENGINE, s.PROCEDURE_NAME);

-- ---------------------------------------------------------------------------
-- One line of the execution log (Run.Logs). Step procedures call it too.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE GEA.CONTROL.LOG(CONTRACT_ID VARCHAR, STEP_KEY VARCHAR, LEVEL VARCHAR, MESSAGE VARCHAR, DETAIL VARIANT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
BEGIN
    INSERT INTO GEA.CONTROL.RUN_LOG (CONTRACT_ID, STEP_KEY, LEVEL, MESSAGE, DETAIL)
    SELECT :CONTRACT_ID, :STEP_KEY, :LEVEL, :MESSAGE, :DETAIL;
    RETURN 'logged';
END;
$$;

-- ---------------------------------------------------------------------------
-- Root of the graph: the contract is known, the run is 'running', its steps are 'pending'.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE GEA.CONTROL.BEGIN_RUN(CONTRACT_ID VARCHAR, GRAPH_RUN_GROUP_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    known INTEGER;
    unknown_contract EXCEPTION (-20001, 'The contract has not been delivered to GEA.CONTROL.DATA_CONTRACT.');
BEGIN
    SELECT COUNT(*) INTO :known FROM GEA.CONTROL.DATA_CONTRACT WHERE CONTRACT_ID = :CONTRACT_ID;
    IF (known = 0) THEN
        RAISE unknown_contract;
    END IF;

    -- The relay may already have written a cancel request for this contract: keep it.
    MERGE INTO GEA.CONTROL.RUN_STATUS t
    USING (SELECT :CONTRACT_ID AS CONTRACT_ID) s
       ON t.CONTRACT_ID = s.CONTRACT_ID
     WHEN MATCHED THEN UPDATE SET
          STATUS = 'running', STARTED_AT = COALESCE(t.STARTED_AT, CURRENT_TIMESTAMP()), FINISHED_AT = NULL,
          FAILURE_MESSAGE = NULL, GRAPH_RUN_GROUP_ID = :GRAPH_RUN_GROUP_ID, UPDATED_AT = CURRENT_TIMESTAMP()
     WHEN NOT MATCHED THEN
          INSERT (CONTRACT_ID, STATUS, STARTED_AT, GRAPH_RUN_GROUP_ID)
          VALUES (s.CONTRACT_ID, 'running', CURRENT_TIMESTAMP(), :GRAPH_RUN_GROUP_ID);

    INSERT INTO GEA.CONTROL.RUN_STEP_STATUS (CONTRACT_ID, STEP_KEY, ORDINAL, STATUS)
    SELECT s.CONTRACT_ID, s.STEP_KEY, s.ORDINAL, 'pending'
    FROM GEA.CONTROL.DATA_CONTRACT_STEP s
    WHERE s.CONTRACT_ID = :CONTRACT_ID
      AND NOT EXISTS (SELECT 1 FROM GEA.CONTROL.RUN_STEP_STATUS x
                      WHERE x.CONTRACT_ID = s.CONTRACT_ID AND x.STEP_KEY = s.STEP_KEY);

    CALL GEA.CONTROL.LOG(:CONTRACT_ID, NULL, 'info', 'Execution started.', NULL);
    RETURN 'running';
END;
$$;

-- ---------------------------------------------------------------------------
-- One step. Called by the task of that step.
--   * a cancel request stops the run here, before the step starts;
--   * the status row says 'running' before the step procedure is called and
--     'complete' or 'failed' after it, so the state is never only in a session;
--   * an error fails the task, and the tasks after it in the graph do not run.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE GEA.CONTROL.RUN_STEP(CONTRACT_ID VARCHAR, STEP_KEY VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    procedure_name VARCHAR;
    cancel_requested BOOLEAN DEFAULT FALSE;
    result VARIANT;
    failure VARCHAR;
    run_cancelled EXCEPTION (-20002, 'The run was cancelled.');
BEGIN
    -- Every statement of the step shows up in QUERY_HISTORY under the contract and the step.
    EXECUTE IMMEDIATE 'ALTER SESSION SET QUERY_TAG = ''gea:' || :CONTRACT_ID || ':' || :STEP_KEY || '''';

    SELECT CANCEL_REQUESTED_AT IS NOT NULL INTO :cancel_requested
    FROM GEA.CONTROL.RUN_STATUS WHERE CONTRACT_ID = :CONTRACT_ID;
    IF (cancel_requested) THEN
        UPDATE GEA.CONTROL.RUN_STEP_STATUS SET STATUS = 'skipped', UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = :STEP_KEY;
        RAISE run_cancelled;
    END IF;

    SELECT PROCEDURE_NAME INTO :procedure_name FROM GEA.CONTROL.PIPELINE_STEP WHERE STEP_KEY = :STEP_KEY;

    UPDATE GEA.CONTROL.RUN_STEP_STATUS
    SET STATUS = 'running', STARTED_AT = CURRENT_TIMESTAMP(), FINISHED_AT = NULL, DETAIL = NULL, UPDATED_AT = CURRENT_TIMESTAMP()
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = :STEP_KEY;
    UPDATE GEA.CONTROL.RUN_STATUS SET UPDATED_AT = CURRENT_TIMESTAMP() WHERE CONTRACT_ID = :CONTRACT_ID;

    BEGIN
        EXECUTE IMMEDIATE 'CALL ' || :procedure_name || '(?)' USING (CONTRACT_ID);
        SELECT $1 INTO :result FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    EXCEPTION
        WHEN OTHER THEN
            failure := SQLERRM;
            UPDATE GEA.CONTROL.RUN_STEP_STATUS
            SET STATUS = 'failed', FINISHED_AT = CURRENT_TIMESTAMP(), UPDATED_AT = CURRENT_TIMESTAMP(),
                DETAIL = OBJECT_CONSTRUCT('error', :failure)
            WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = :STEP_KEY;
            CALL GEA.CONTROL.LOG(:CONTRACT_ID, :STEP_KEY, 'error', :failure, NULL);
            RAISE;                                   -- the task fails: the steps after it are not started
    END;

    UPDATE GEA.CONTROL.RUN_STEP_STATUS
    SET STATUS = 'complete', FINISHED_AT = CURRENT_TIMESTAMP(), UPDATED_AT = CURRENT_TIMESTAMP(), DETAIL = :result
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = :STEP_KEY;
    UPDATE GEA.CONTROL.RUN_STATUS SET UPDATED_AT = CURRENT_TIMESTAMP() WHERE CONTRACT_ID = :CONTRACT_ID;
    RETURN result;
END;
$$;

-- ---------------------------------------------------------------------------
-- Finalizer of the graph: runs whether the steps succeeded or not, and writes the
-- one terminal status the relay copies to PostgreSQL.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE GEA.CONTROL.END_RUN(CONTRACT_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    unfinished INTEGER;
    outcome VARCHAR;
BEGIN
    -- What never started is skipped; a step that was left 'running' did not finish.
    UPDATE GEA.CONTROL.RUN_STEP_STATUS SET STATUS = 'skipped', UPDATED_AT = CURRENT_TIMESTAMP()
    WHERE CONTRACT_ID = :CONTRACT_ID AND STATUS = 'pending';
    UPDATE GEA.CONTROL.RUN_STEP_STATUS
    SET STATUS = 'failed', FINISHED_AT = CURRENT_TIMESTAMP(), UPDATED_AT = CURRENT_TIMESTAMP(),
        DETAIL = OBJECT_CONSTRUCT('error', 'The step did not finish.')
    WHERE CONTRACT_ID = :CONTRACT_ID AND STATUS = 'running';

    SELECT COUNT_IF(STATUS NOT IN ('complete', 'reused')) INTO :unfinished
    FROM GEA.CONTROL.RUN_STEP_STATUS WHERE CONTRACT_ID = :CONTRACT_ID;
    outcome := IFF(unfinished = 0, 'complete', 'failed');

    UPDATE GEA.CONTROL.RUN_STATUS r
    SET STATUS = :outcome, FINISHED_AT = CURRENT_TIMESTAMP(), UPDATED_AT = CURRENT_TIMESTAMP(),
        FAILURE_MESSAGE = CASE
            WHEN :outcome = 'complete' THEN NULL
            WHEN r.CANCEL_REQUESTED_AT IS NOT NULL THEN 'Cancelled by ' || COALESCE(r.CANCEL_REQUESTED_BY, 'a user') || '.'
            ELSE COALESCE((SELECT 'Step ' || MIN_BY(s.STEP_KEY, s.ORDINAL) || ' failed: ' || MIN_BY(s.DETAIL:error::STRING, s.ORDINAL)
                           FROM GEA.CONTROL.RUN_STEP_STATUS s
                           WHERE s.CONTRACT_ID = :CONTRACT_ID AND s.STATUS = 'failed'),
                          'The run did not finish.') END
    WHERE r.CONTRACT_ID = :CONTRACT_ID;

    CALL GEA.CONTROL.LOG(:CONTRACT_ID, NULL, IFF(:outcome = 'complete', 'info', 'error'), 'Execution ' || :outcome || '.', NULL);
    RETURN outcome;
END;
$$;

-- ---------------------------------------------------------------------------
-- The steps: where the calculation lives. These are placeholders that do nothing,
-- so that the pipeline can be exercised end to end before the modelling logic exists.
-- The rules every step procedure keeps:
--   * signature (CONTRACT_ID VARCHAR) RETURNS VARIANT; it reads its own configuration
--     from GEA.CONTROL.DATA_CONTRACT_STEP and nothing from anywhere else;
--   * it reads the output of earlier steps by CONTRACT_ID and writes its own by CONTRACT_ID;
--   * it deletes this contract's rows before it writes, so running it again replaces them;
--   * it returns what it did (row counts), which becomes RUN_STEP_STATUS.DETAIL.
-- A step SQL cannot express is a Snowpark Python procedure with the same signature
-- (LANGUAGE PYTHON, HANDLER = ...): registered in PIPELINE_STEP with ENGINE
-- 'snowpark-python' and called exactly like the others. The data does not leave Snowflake.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE GEA.STEP.SEGMENTATION(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'segmentation';

    -- DELETE FROM GEA.RESULT.EXPOSURE_SEGMENT WHERE CONTRACT_ID = :CONTRACT_ID;
    -- INSERT INTO GEA.RESULT.EXPOSURE_SEGMENT (CONTRACT_ID, ...)
    -- SELECT :CONTRACT_ID, ...                           -- the modelling team's SQL, driven by
    -- FROM ... WHERE ... = :config:exposureMethod::STRING;   -- config:exposureMethod, config:policyTenureSegmentation ...

    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE, 'exposureMethod', config:exposureMethod);
END;
$$;

-- The other six, the same placeholder under their own names.

CREATE OR REPLACE PROCEDURE GEA.STEP.DATA_AND_SET_UP(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'dataAndSetUp';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

CREATE OR REPLACE PROCEDURE GEA.STEP.ACTUALS(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'actuals';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

CREATE OR REPLACE PROCEDURE GEA.STEP.IBNR(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'ibnr';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

CREATE OR REPLACE PROCEDURE GEA.STEP.ASSIGNING_EXPECTED(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'assigningExpected';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

CREATE OR REPLACE PROCEDURE GEA.STEP.ULTIMATE_CALCULATION(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'ultimateCalculation';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

CREATE OR REPLACE PROCEDURE GEA.STEP.ACTUAL_EXPECTED(CONTRACT_ID VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    config VARIANT;
BEGIN
    SELECT CONFIG INTO :config FROM GEA.CONTROL.DATA_CONTRACT_STEP
    WHERE CONTRACT_ID = :CONTRACT_ID AND STEP_KEY = 'actualExpected';
    RETURN OBJECT_CONSTRUCT('rows', 0, 'implemented', FALSE);
END;
$$;

-- ---------------------------------------------------------------------------
-- The order of the steps: a task graph. One chain today, because each step of the
-- workbook reads what the one before it wrote. Two steps that do not depend on each
-- other get the same predecessor (AFTER x) and Snowflake runs them side by side; the
-- step that needs both lists both (AFTER a, b).
--
-- GEA_WH is the warehouse the steps run on; its size and its concurrency are the limit
-- on how much runs at once inside Snowflake. How many runs of a JOB start at once is
-- decided earlier, in PostgreSQL (gea."Job"."MaxParallel").
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TASK GEA.CONTROL.RUN_PIPELINE
    WAREHOUSE = GEA_WH
    OVERLAP_POLICY = ALLOW_ALL_OVERLAP          -- a graph run per contract; runs of different contracts overlap
    CONFIG = '{"contractId": null}'              -- overridden by EXECUTE TASK ... USING CONFIG
AS
    CALL GEA.CONTROL.BEGIN_RUN(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'),
                               SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_GRAPH_RUN_GROUP_ID'));

CREATE OR REPLACE TASK GEA.CONTROL.STEP_DATA_AND_SET_UP
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.RUN_PIPELINE
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'dataAndSetUp');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_SEGMENTATION
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_DATA_AND_SET_UP
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'segmentation');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_ACTUALS
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_SEGMENTATION
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'actuals');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_IBNR
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_ACTUALS
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'ibnr');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_ASSIGNING_EXPECTED
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_IBNR
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'assigningExpected');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_ULTIMATE_CALCULATION
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_ASSIGNING_EXPECTED
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'ultimateCalculation');

CREATE OR REPLACE TASK GEA.CONTROL.STEP_ACTUAL_EXPECTED
    WAREHOUSE = GEA_WH
    AFTER GEA.CONTROL.STEP_ULTIMATE_CALCULATION
AS
    CALL GEA.CONTROL.RUN_STEP(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'), 'actualExpected');

CREATE OR REPLACE TASK GEA.CONTROL.RUN_PIPELINE_END
    WAREHOUSE = GEA_WH
    FINALIZE = GEA.CONTROL.RUN_PIPELINE
AS
    CALL GEA.CONTROL.END_RUN(SYSTEM$GET_TASK_GRAPH_CONFIG('contractId'));

-- The child tasks have to be resumed once; the root has no schedule and runs only when the relay executes it.
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('GEA.CONTROL.RUN_PIPELINE');

-- ---------------------------------------------------------------------------
-- What the relay does (role gea_relay in PostgreSQL, a service user here). Not built yet.
--
--   deliver   claim in PostgreSQL -> MERGE into DATA_CONTRACT -> read CONTENT_HASH back
--             -> EXECUTE TASK GEA.CONTROL.RUN_PIPELINE USING CONFIG = '{"contractId": "..."}'
--             -> gea."CompleteContractDelivery". A crash before the last call repeats all of it:
--             the MERGE and BEGIN_RUN are idempotent.
--   observe   every few seconds, for the contracts PostgreSQL shows as queued or running:
--               SELECT ... FROM RUN_STATUS / RUN_STEP_STATUS     -> gea."RecordExecutionStatus"
--               SELECT ... FROM RUN_LOG WHERE OCCURRED_AT > last  -> gea."RecordExecutionLog"
--             A report is also the heartbeat. A contract whose graph run is no longer executing
--             (TASK_HISTORY by GRAPH_RUN_GROUP_ID) and that has no terminal RUN_STATUS is reported
--             as failed at once; after every poll in which Snowflake answered the relay calls
--             gea."FailStaleExecutions" for what it could not account for.
--   cancel    rows of gea."RunCancelRequest" -> MERGE into RUN_STATUS (CANCEL_REQUESTED_AT, _BY).
--             RUN_STEP stops before the next step; to stop a long step at once the relay can also
--             cancel the statements tagged gea:<contractId>:* (SYSTEM$CANCEL_QUERY).
--   re-run    a failed run that is fixed and submitted again is a NEW contract and a new graph run.
--             Repeating the same contract from the step that failed (the workbook's status
--             'pending-rerun') is EXECUTE TASK GEA.CONTROL.RUN_PIPELINE RETRY GRAPH RUN GROUP '<id>'.
--
-- Later, not in this draft: step reuse (the workbook's status 'reused'). A step whose
-- configuration, upstream results and input data version equal those of an earlier contract
-- need not run again; that needs a fingerprint per step in RUN_STEP_STATUS and results that
-- can be addressed by it, and an input data version in the contract (Run.DataVersion).
--
-- To validate on a real account, in this order:
--   1. EXECUTE TASK ... USING CONFIG with OVERLAP_POLICY = ALLOW_ALL_OVERLAP: two contracts
--      started one after the other run at the same time, each with its own contractId.
--   2. A root task without a schedule can be created and executed manually.
--   3. RUN_STEP: EXECUTE IMMEDIATE 'CALL ...(?)' USING, then RESULT_SCAN(LAST_QUERY_ID()) returns
--      the VARIANT of the step procedure; ALTER SESSION SET QUERY_TAG is allowed in a caller's
--      rights procedure that a task calls.
--   4. A failed step fails its task, the later tasks do not run, and the finalizer still does.
--   5. The grants: the relay's role needs OPERATE on RUN_PIPELINE, INSERT and SELECT on
--      DATA_CONTRACT, SELECT on the status and log tables, UPDATE on RUN_STATUS for cancel.

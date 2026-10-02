-- Snowflake side of the data contract hand-over.
-- STATUS: draft, NOT executed. Written against Snowflake SQL; it has not been run
-- on a Snowflake account or on the local emulator in this repository (which
-- supports scalar SQL functions and plain DDL/DML only - no VARIANT, FLATTEN,
-- tasks or procedures). Review with the Snowflake owner before use.
--
-- Flow:
--   1. The GEA relay (gea."ClaimContractDeliveries" in PostgreSQL) sends one
--      contract with the MERGE below. The statement is idempotent, so a retry
--      after a timeout or a crashed relay cannot create a second row.
--   2. The relay reads the row back and compares CONTENT_HASH, starts the run
--      (V002: EXECUTE TASK GEA.CONTROL.RUN_PIPELINE USING CONFIG) and only then
--      marks the delivery as delivered in PostgreSQL.
--   3. The pipeline of V002 executes the steps of DATA_CONTRACT_STEP and writes
--      RUN_STATUS, RUN_STEP_STATUS and RUN_LOG. The relay copies them to
--      PostgreSQL (gea."RecordExecutionStatus", gea."RecordExecutionLog").
--
-- PostgreSQL decides WHEN a contract is sent (on its own, or in the order and
-- with the parallelism of its job); everything that is computed is computed here.
--
-- Names here are Snowflake's (upper case, underscores); PostgreSQL uses the
-- workbook's PascalCase names. The document itself is identical on both sides.

CREATE SCHEMA IF NOT EXISTS GEA.CONTROL;

-- Insert-only. Grant the relay role INSERT and SELECT, never UPDATE or DELETE.
CREATE TABLE IF NOT EXISTS GEA.CONTROL.DATA_CONTRACT (
    CONTRACT_ID         VARCHAR(36)  NOT NULL,
    PROJECT_ID          VARCHAR(36)  NOT NULL,
    RUN_ID              VARCHAR(36)  NOT NULL,
    VERSION             INTEGER      NOT NULL,
    SPEC_VERSION        VARCHAR      NOT NULL,
    CONTENT_HASH        VARCHAR(64)  NOT NULL,   -- SHA-256 of DOCUMENT_CANONICAL
    DOCUMENT_CANONICAL  VARCHAR      NOT NULL,   -- exact bytes hashed in PostgreSQL
    DOCUMENT            VARIANT      NOT NULL,   -- PARSE_JSON(DOCUMENT_CANONICAL), for querying
    RECEIVED_AT         TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT DATA_CONTRACT_PK PRIMARY KEY (CONTRACT_ID),       -- informational in Snowflake;
    CONSTRAINT DATA_CONTRACT_RUN_VERSION UNIQUE (RUN_ID, VERSION) -- uniqueness comes from the MERGE
);

-- Delivery statement used by the relay (bind parameters in this order:
-- contract_id, project_id, run_id, version, content_hash, document_canonical).
-- The hash is re-computed here, so a document corrupted in transit is not stored.
--
-- MERGE INTO GEA.CONTROL.DATA_CONTRACT t
-- USING (SELECT %s AS CONTRACT_ID, %s AS PROJECT_ID, %s AS RUN_ID, %s AS VERSION,
--               %s AS CONTENT_HASH, %s AS DOCUMENT_CANONICAL) s
--    ON t.CONTRACT_ID = s.CONTRACT_ID
-- WHEN NOT MATCHED AND SHA2(s.DOCUMENT_CANONICAL, 256) = s.CONTENT_HASH THEN
--     INSERT (CONTRACT_ID, PROJECT_ID, RUN_ID, VERSION, SPEC_VERSION,
--             CONTENT_HASH, DOCUMENT_CANONICAL, DOCUMENT)
--     VALUES (s.CONTRACT_ID, s.PROJECT_ID, s.RUN_ID, s.VERSION,
--             PARSE_JSON(s.DOCUMENT_CANONICAL):specVersion::STRING,
--             s.CONTENT_HASH, s.DOCUMENT_CANONICAL, PARSE_JSON(s.DOCUMENT_CANONICAL));
--
-- Read-back used by the relay before it reports success:
-- SELECT CONTENT_HASH FROM GEA.CONTROL.DATA_CONTRACT WHERE CONTRACT_ID = %s;

-- One row per step of a contract, in execution order.
CREATE OR REPLACE VIEW GEA.CONTROL.DATA_CONTRACT_STEP AS
SELECT c.CONTRACT_ID,
       c.PROJECT_ID,
       c.RUN_ID,
       c.VERSION,
       s.value:key::STRING      AS STEP_KEY,
       s.value:ordinal::INTEGER AS ORDINAL,
       s.value:config           AS CONFIG
FROM GEA.CONTROL.DATA_CONTRACT c,
     LATERAL FLATTEN(input => c.DOCUMENT:steps) s;

-- Written by the pipeline of V002; read by the relay. One row per contract.
CREATE TABLE IF NOT EXISTS GEA.CONTROL.RUN_STATUS (
    CONTRACT_ID          VARCHAR(36)  NOT NULL,
    STATUS               VARCHAR      NOT NULL,   -- queued | running | complete | failed
    STARTED_AT           TIMESTAMP_TZ,
    FINISHED_AT          TIMESTAMP_TZ,
    FAILURE_MESSAGE      VARCHAR,
    GRAPH_RUN_GROUP_ID   VARCHAR,                 -- the task graph run that executes the contract (for TASK_HISTORY and RETRY)
    CANCEL_REQUESTED_AT  TIMESTAMP_TZ,            -- written by the relay; RUN_STEP stops before the next step
    CANCEL_REQUESTED_BY  VARCHAR,
    UPDATED_AT           TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT RUN_STATUS_PK PRIMARY KEY (CONTRACT_ID)
);

-- One row per contract and step, in the workbook's step vocabulary.
CREATE TABLE IF NOT EXISTS GEA.CONTROL.RUN_STEP_STATUS (
    CONTRACT_ID  VARCHAR(36)  NOT NULL,
    STEP_KEY     VARCHAR      NOT NULL,
    ORDINAL      INTEGER      NOT NULL,
    STATUS       VARCHAR      NOT NULL,   -- pending | running | complete | failed | skipped | reused | pending-rerun
    STARTED_AT   TIMESTAMP_TZ,
    FINISHED_AT  TIMESTAMP_TZ,
    DETAIL       VARIANT,                 -- what the step procedure returned: row counts; or the error
    UPDATED_AT   TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT RUN_STEP_STATUS_PK PRIMARY KEY (CONTRACT_ID, STEP_KEY)
);

-- Run.Logs. LOG_ID is what makes the copy to PostgreSQL idempotent (gea."RunLog"."ExternalId").
CREATE TABLE IF NOT EXISTS GEA.CONTROL.RUN_LOG (
    LOG_ID       VARCHAR(36)  NOT NULL DEFAULT UUID_STRING(),
    CONTRACT_ID  VARCHAR(36)  NOT NULL,
    STEP_KEY     VARCHAR,
    LEVEL        VARCHAR      NOT NULL DEFAULT 'info',   -- info | warning | error
    MESSAGE      VARCHAR      NOT NULL,
    DETAIL       VARIANT,
    OCCURRED_AT  TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT RUN_LOG_PK PRIMARY KEY (LOG_ID)
);

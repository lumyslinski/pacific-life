-- 0004 privileges: least-privilege grants for the two group roles.
--
--   gea_app    Litestar API: reads everything, writes users, projects and runs,
--              inserts contracts. No UPDATE or DELETE on immutable tables.
--   gea_relay  delivery and status worker: only the functions it needs.
--
-- Both are NOLOGIN group roles; the platform (Terraform/RDS) creates the login
-- users and makes them members. The roles are created here when they are
-- missing, which needs CREATEROLE; without it, create them first.
--
-- Rule for later revisions: a revision that adds a table, sequence or function
-- also grants on it. There is no repeatable grants script.
DO $$
DECLARE
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['gea_app', 'gea_relay'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            BEGIN
                EXECUTE format('CREATE ROLE %I NOLOGIN', v_role);
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE EXCEPTION 'role % does not exist and the migration user may not create roles', v_role
                    USING HINT = format('Run CREATE ROLE %I NOLOGIN as an administrator, then upgrade again.', v_role);
            END;
        END IF;
    END LOOP;
END $$;

-- The worker functions are SECURITY DEFINER (they maintain tables no role may
-- write directly), so they must not be callable by PUBLIC.
REVOKE ALL ON FUNCTION gea."ClaimContractDeliveries"(text, integer, interval),
                       gea."CompleteContractDelivery"(uuid, text, text, text),
                       gea."FailContractDelivery"(uuid, text, text, text, interval, integer),
                       gea."RetryContractDelivery"(uuid, text),
                       gea."RecordExecutionStatus"(uuid, text, jsonb, text) FROM PUBLIC;

GRANT USAGE ON SCHEMA gea TO gea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA gea TO gea_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA gea TO gea_app;
GRANT INSERT ON gea."User", gea."CommandReceipt", gea."AuditEvent", gea."Project", gea."ProjectBenefit",
                gea."Run", gea."RunStudyPeriodExclusion", gea."DataContract" TO gea_app;
GRANT UPDATE ON gea."User", gea."Project", gea."Run" TO gea_app;
GRANT DELETE ON gea."ProjectBenefit", gea."Run", gea."RunStudyPeriodExclusion" TO gea_app;
-- "ContractDelivery" and "RunExecution*" stay read-only here: they are written
-- only by SECURITY DEFINER triggers and functions.
GRANT EXECUTE ON FUNCTION gea."RetryContractDelivery"(uuid, text) TO gea_app;

GRANT USAGE ON SCHEMA gea TO gea_relay;
GRANT SELECT ON gea."DataContract", gea."ContractDelivery", gea."RunExecution",
                gea."RunExecutionStep", gea."Run" TO gea_relay;
GRANT EXECUTE ON FUNCTION gea."ClaimContractDeliveries"(text, integer, interval),
                          gea."CompleteContractDelivery"(uuid, text, text, text),
                          gea."FailContractDelivery"(uuid, text, text, text, interval, integer),
                          gea."RecordExecutionStatus"(uuid, text, jsonb, text) TO gea_relay;

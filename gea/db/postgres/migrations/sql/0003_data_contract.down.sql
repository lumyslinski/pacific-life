-- 0003 data contract, downgrade.
-- A published contract is an immutable record, so it is never dropped as a side
-- effect: the downgrade stops while one exists. Archive and remove them first.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM gea."DataContract") THEN
        RAISE EXCEPTION 'gea."DataContract" holds published contracts; refusing to drop them'
            USING HINT = 'Export the contracts, then remove them as the table owner before downgrading below 0003.';
    END IF;
END $$;

DROP VIEW gea."RunSummary";
DROP VIEW gea."ProjectSummary";
DROP FUNCTION gea."RecordExecutionStatus"(uuid, text, jsonb, text);
DROP FUNCTION gea."RetryContractDelivery"(uuid, text);
DROP FUNCTION gea."FailContractDelivery"(uuid, text, text, text, interval, integer);
DROP FUNCTION gea."CompleteContractDelivery"(uuid, text, text, text);
DROP FUNCTION gea."ClaimContractDeliveries"(text, integer, interval);
DROP TABLE gea."RunExecutionStep";
DROP TABLE gea."RunExecution";
ALTER TABLE gea."Run" DROP CONSTRAINT "FK_Run_CurrentContract";
DROP TABLE gea."ContractDelivery";
DROP TABLE gea."DataContract";
DROP FUNCTION gea."DataContract_AfterInsert"();
DROP FUNCTION gea."DataContract_BeforeInsert"();
DROP FUNCTION gea."BuildContractDocument"(uuid, uuid, uuid, timestamptz);

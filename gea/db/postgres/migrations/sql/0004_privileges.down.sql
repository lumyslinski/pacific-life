-- 0004 privileges, downgrade. The roles are cluster-wide and may have members,
-- so they are left in place.
REVOKE ALL ON ALL TABLES IN SCHEMA gea FROM gea_app, gea_relay;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA gea FROM gea_app, gea_relay;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA gea FROM gea_app, gea_relay;
REVOKE ALL ON SCHEMA gea FROM gea_app, gea_relay;
GRANT EXECUTE ON FUNCTION gea."ClaimContractDeliveries"(text, integer, interval),
                          gea."CompleteContractDelivery"(uuid, text, text, text),
                          gea."FailContractDelivery"(uuid, text, text, text, interval, integer),
                          gea."RetryContractDelivery"(uuid, text),
                          gea."RecordExecutionStatus"(uuid, text, jsonb, text) TO PUBLIC;

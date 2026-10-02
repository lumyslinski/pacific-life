-- 0001 baseline, downgrade.
DROP TABLE gea."AuditEvent";
DROP TABLE gea."CommandReceipt";
DROP TABLE gea."User";
DROP TABLE gea."Benefit";
DROP TABLE gea."BusinessPurpose";
DROP TABLE gea."Region";
DROP FUNCTION gea."ForbidMutation"();
DROP FUNCTION gea."TouchUpdatedAt"();

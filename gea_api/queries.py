"""The SQL of the API.

Conventions that keep the Python thin and the SQL checkable on its own:

* Tables and columns carry the workbook's names (gea."Project"."BusinessPurposeId"),
  so every identifier is in double quotes.
* A query that reads a resource returns one column `body`: the JSON as the API
  sends it, built by PostgreSQL (the workbook's prop names, timestamps as ISO 8601).
  A run also returns `configuration`, its createRun.* props (gea."RunConfiguration").
* Every parameter is a string, integer, boolean or NULL and is cast in the SQL
  (`%(id)s::uuid`), so the statement means the same whatever the driver sends.
  Lists and objects travel as JSON text (`%(benefits)s::jsonb`).
* A literal percent sign is written `%%` (psycopg placeholder syntax).
* A member of a body is always there; one without a value is JSON null.

The UPDATE of a run is not here: gea_api/runconfig.py builds it from the props
that are being changed (one column per prop, see gea_api/fields.py).
"""

# ---------------------------------------------------------------------------- users, health
# A new user starts in the region the identity provider names, or in 'global'.
# The caller's row and what its role allows. A user seen for the first time gets the region and
# the role the identity provider names, when they exist, and otherwise 'global' and 'viewer'.
ENSURE_USER = """
WITH existing AS (
    SELECT "Id", "RoleId" FROM gea."User" WHERE "Subject" = %(subject)s::text
), created AS (
    INSERT INTO gea."User" ("Subject", "Name", "Email", "RegionId", "RoleId")
    SELECT %(subject)s::text, %(name)s::text, %(email)s::text,
           coalesce((SELECT "Id" FROM gea."Region" WHERE "Id" = %(region)s::text AND "IsActive"), 'global'),
           coalesce((SELECT "Id" FROM gea."Role" WHERE "Id" = %(role)s::text AND "IsActive"), 'viewer')
    WHERE NOT EXISTS (SELECT 1 FROM existing)
    ON CONFLICT ("Subject") DO UPDATE SET "Name" = EXCLUDED."Name"
    RETURNING "Id", "RoleId"
)
SELECT u."Id"::text AS id, o."Id" AS role, o."Name" AS role_name,
       o."CanPrepare" AS prepare, o."CanReview" AS review, o."CanAdminister" AS administer
FROM (SELECT "Id", "RoleId" FROM existing UNION ALL SELECT "Id", "RoleId" FROM created) u
JOIN gea."Role" o ON o."Id" = u."RoleId"
"""

_PERMISSIONS = """jsonb_build_object('prepare', o."CanPrepare", 'review', o."CanReview", 'administer', o."CanAdminister")"""

GET_USER = f"""
SELECT jsonb_build_object('id', u."Id", 'name', u."Name", 'email', u."Email",
                          'region', u."RegionId", 'regionName', r."Name",
                          'role', u."RoleId", 'roleName', o."Name", 'permissions', {_PERMISSIONS}) AS body
FROM gea."User" u
JOIN gea."Region" r ON r."Id" = u."RegionId"
JOIN gea."Role" o ON o."Id" = u."RoleId"
WHERE u."Id" = %(id)s::uuid
"""

# A person, not the seeded 'system' user: that one is not listed and cannot be administered.
PERSON_EXISTS = """SELECT 1 AS found FROM gea."User" WHERE "Id" = %(id)s::uuid AND "Subject" <> 'system'"""
LOCK_PERSON = PERSON_EXISTS + " FOR UPDATE"

UPDATE_USER_REGION = """
UPDATE gea."User" SET "RegionId" = %(region)s::text WHERE "Id" = %(id)s::uuid
"""

UPDATE_USER_ROLE = """
UPDATE gea."User" SET "RoleId" = %(role)s::text WHERE "Id" = %(id)s::uuid
"""

# The people a project can be given to and an Admin manages; the seeded 'system' user is not one of them.
LIST_USERS = """
SELECT jsonb_build_object('id', u."Id", 'name', u."Name", 'region', u."RegionId", 'regionName', r."Name",
                          'role', u."RoleId", 'roleName', o."Name") AS body,
       u."CreatedAt"::text AS cursor_time, u."Id"::text AS cursor_id
FROM gea."User" u
JOIN gea."Region" r ON r."Id" = u."RegionId"
JOIN gea."Role" o ON o."Id" = u."RoleId"
WHERE u."Subject" <> 'system'
  AND (%(role)s::text IS NULL OR u."RoleId" = %(role)s::text)
  AND (%(region)s::text IS NULL OR u."RegionId" = %(region)s::text)
  AND (%(q)s::text IS NULL OR u."Name" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\')
  AND (%(after_time)s::timestamptz IS NULL
       OR u."CreatedAt" < %(after_time)s::timestamptz
       OR (u."CreatedAt" = %(after_time)s::timestamptz AND u."Id" > %(after_id)s::uuid))
ORDER BY u."CreatedAt" DESC, u."Id"
LIMIT %(limit)s::int
"""

ROLES = f"""
SELECT coalesce(jsonb_agg(jsonb_build_object('id', o."Id", 'name', o."Name", 'permissions', {_PERMISSIONS})
                          ORDER BY o."SortOrder", o."Name"), '[]'::jsonb) AS body
FROM gea."Role" o
WHERE o."IsActive"
"""

ROLE_EXISTS = """SELECT 1 AS found FROM gea."Role" WHERE "Id" = %(id)s::text AND "IsActive" = true"""

HEALTH = """
SELECT jsonb_build_object('status', 'ok', 'service', 'gea-api', 'specVersion', %(spec_version)s::text) AS body
FROM gea."Region"
LIMIT 1
"""

AUDIT = """
INSERT INTO gea."AuditEvent" ("AggregateType", "AggregateId", "EventType", "Payload", "ActorId", "RequestId")
VALUES (%(type)s::text, %(id)s::uuid, %(event)s::text, %(payload)s::jsonb, %(user)s::uuid, %(request_id)s::text)
"""

# ---------------------------------------------------------------------------- idempotency
FIND_RECEIPT = """
SELECT jsonb_build_object('operation', "Operation", 'requestHash', "RequestHash"::text, 'status', "ResponseStatus",
                          'headers', "ResponseHeaders", 'body', "ResponseBody") AS body
FROM gea."CommandReceipt"
WHERE "CreatedBy" = %(user)s::uuid AND "IdempotencyKey" = %(key)s::text
"""

STORE_RECEIPT = """
INSERT INTO gea."CommandReceipt" ("IdempotencyKey", "Operation", "RequestHash", "ResponseStatus",
                                 "ResponseHeaders", "ResponseBody", "CreatedBy")
VALUES (%(key)s::text, %(operation)s::text, %(hash)s::text, %(status)s::int,
        %(headers)s::jsonb, %(body)s::jsonb, %(user)s::uuid)
"""

# ---------------------------------------------------------------------------- reference data
# The lists the forms choose from. Regions, business purposes and benefits are lookup tables;
# treaties and datasets are rows of gea."ParameterOption" until they are loaded from Snowflake Module 1.
_LOOKUP = """
SELECT coalesce(jsonb_agg(jsonb_build_object('id', "Id", 'name', "Name") ORDER BY "SortOrder", "Name"), '[]'::jsonb) AS body
FROM gea."{table}"
WHERE "IsActive"
"""
REGIONS = _LOOKUP.format(table="Region")
BUSINESS_PURPOSES = _LOOKUP.format(table="BusinessPurpose")
BENEFITS = _LOOKUP.format(table="Benefit")

TREATIES = """
SELECT coalesce(jsonb_agg(jsonb_build_object('id', t."Value", 'name', t."Label") ORDER BY t."SortOrder", t."Value"),
                '[]'::jsonb) AS body
FROM (
    SELECT DISTINCT ON (o."Value") o."Value", o."Label", o."SortOrder"
    FROM gea."ParameterOption" o
    WHERE o."Parameter" = 'treaty' AND o."IsActive"
      AND (%(region)s::text IS NULL OR o."RegionId" IS NULL OR o."RegionId" = %(region)s::text)
      AND (%(q)s::text IS NULL OR o."Value" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\'
                               OR o."Label" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\')
    ORDER BY o."Value", o."RegionId" NULLS LAST
) t
"""

REGION_EXISTS = """SELECT 1 AS found FROM gea."Region" WHERE "Id" = %(id)s::text AND "IsActive" = true"""

# ---------------------------------------------------------------------------- run parameters (dropdown values)
# Region, business purpose and benefits of a project: the scope of its run dropdowns.
PROJECT_SCOPE = """
SELECT jsonb_build_object('region', p."RegionId", 'businessPurpose', p."BusinessPurposeId",
                          'benefit', (SELECT coalesce(jsonb_agg(pb."BenefitId"), '[]'::jsonb)
                                      FROM gea."ProjectBenefit" pb WHERE pb."ProjectId" = p."Id")) AS body
FROM gea."Project" p
WHERE p."Id" = %(id)s::uuid
"""

# The values offered for every prop in a scope: { "<prop>": { "options": [...], "default": ... } }.
# A row applies when each of its scope columns is empty or equals the scope. When a value
# is listed more than once, the row with the narrowest scope is the one that counts.
PARAMETER_OPTIONS = """
WITH scoped AS (
    SELECT DISTINCT ON (o."Parameter", o."Value")
           o."Parameter", o."Value", o."Label", o."SortOrder", o."IsDefault",
           (o."RegionId" IS NOT NULL)::int + (o."BusinessPurposeId" IS NOT NULL)::int
               + (o."BenefitId" IS NOT NULL)::int + (o."Investigation" IS NOT NULL)::int AS "Specificity"
    FROM gea."ParameterOption" o
    WHERE o."IsActive"
      AND (o."RegionId" IS NULL OR o."RegionId" = %(region)s::text)
      AND (o."BusinessPurposeId" IS NULL OR o."BusinessPurposeId" = %(business_purpose)s::text)
      AND (o."BenefitId" IS NULL
           OR o."BenefitId" IN (SELECT jsonb_array_elements_text(coalesce(%(benefits)s::jsonb, '[]'::jsonb))))
      AND (o."Investigation" IS NULL OR o."Investigation" = %(investigation)s::text)
    ORDER BY o."Parameter", o."Value", 6 DESC
), per_parameter AS (
    SELECT s."Parameter",
           jsonb_agg(jsonb_build_object('value', s."Value", 'label', s."Label") ORDER BY s."SortOrder", s."Value") AS "Options",
           (array_agg(s."Value" ORDER BY s."Specificity" DESC, s."SortOrder") FILTER (WHERE s."IsDefault"))[1] AS "Default",
           count(*) AS "Count", min(s."Value") AS "Only"
    FROM scoped s
    GROUP BY s."Parameter"
)
SELECT coalesce(jsonb_object_agg(p."Parameter", jsonb_build_object(
           'options', p."Options",
           -- one applicable value means a pre-populated default (modelling-team sheet)
           'default', coalesce(p."Default", CASE WHEN p."Count" = 1 THEN p."Only" END))), '{}'::jsonb) AS body
FROM per_parameter p
"""

DATASETS = """
SELECT coalesce(jsonb_agg(jsonb_build_object('id', o."Value", 'name', o."Label") ORDER BY o."SortOrder", o."Value"),
                '[]'::jsonb) AS body
FROM gea."ParameterOption" o
WHERE o."Parameter" = 'dataScope' AND o."IsActive"
  AND o."RegionId" IS NULL AND o."BusinessPurposeId" IS NULL AND o."BenefitId" IS NULL AND o."Investigation" IS NULL
"""

# ---------------------------------------------------------------------------- projects
# The members of '04. API Data' (get_project_list), plus benefit and cycle of createProject.
# analyses and portfolioAe are in the workbook's response; those entities do not exist
# yet, so the counter is 0 and the ratio is null.
_PROJECT_BODY = """jsonb_build_object(
    'id', s."Id", 'name', s."Name", 'businessPurpose', s."BusinessPurposeId", 'region', s."RegionId",
    'benefit', to_jsonb(s."Benefits"), 'cycle', s."Period", 'periodFrom', s."PeriodFrom", 'periodTo', s."PeriodTo",
    'state', s."State", 'description', s."Description", 'locked', s."Locked", 'inheritedFrom', s."ParentProjectId",
    'runs', jsonb_build_object('count', s."TotalRuns", 'completed', s."CompletedRuns", 'active', s."ActiveRuns",
                               'failed', s."FailedRuns", 'draft', s."DraftRuns",
                               'unresolvedFailed', s."UnresolvedFailedRuns"),
    'jobs', jsonb_build_object('count', s."JobCount"), 'analyses', jsonb_build_object('count', 0), 'portfolioAe', NULL,
    'owner', jsonb_build_object('id', s."OwnerId", 'name', s."OwnerName"),
    'createdAt', s."CreatedAt", 'updatedAt', s."UpdatedAt", 'signedOffAt', s."SignedOffAt",
    'revision', s."Revision")"""

GET_PROJECT = f"""
SELECT {_PROJECT_BODY} AS body
FROM gea."ProjectSummary" s
WHERE s."Id" = %(id)s::uuid
"""

LOCK_PROJECT = """SELECT 1 AS found FROM gea."Project" WHERE "Id" = %(id)s::uuid FOR UPDATE"""

LIST_PROJECTS = f"""
SELECT {_PROJECT_BODY} AS body, s."CreatedAt"::text AS cursor_time, s."Id"::text AS cursor_id
FROM gea."ProjectSummary" s
WHERE (%(business_purpose)s::text IS NULL OR s."BusinessPurposeId" = %(business_purpose)s::text)
  AND (%(region)s::text IS NULL OR s."RegionId" = %(region)s::text)
  AND (%(state)s::text IS NULL OR s."State" = %(state)s::text)
  AND (%(cycle)s::text IS NULL OR s."Period" = %(cycle)s::text)
  AND (%(q)s::text IS NULL OR s."Name" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\')
  AND (%(after_time)s::timestamptz IS NULL
       OR s."CreatedAt" < %(after_time)s::timestamptz
       OR (s."CreatedAt" = %(after_time)s::timestamptz AND s."Id" > %(after_id)s::uuid))
ORDER BY s."CreatedAt" DESC, s."Id"
LIMIT %(limit)s::int
"""

# Which of the referenced values exist. A NULL parameter is not checked.
CHECK_PROJECT_REFERENCES = """
SELECT jsonb_build_object(
    'region', %(region)s::text IS NULL
              OR EXISTS (SELECT 1 FROM gea."Region" WHERE "Id" = %(region)s::text AND "IsActive"),
    'businessPurpose', %(business_purpose)s::text IS NULL
              OR EXISTS (SELECT 1 FROM gea."BusinessPurpose" WHERE "Id" = %(business_purpose)s::text AND "IsActive"),
    'unknownBenefits', (SELECT coalesce(jsonb_agg(b.id), '[]'::jsonb)
                        FROM jsonb_array_elements_text(coalesce(%(benefits)s::jsonb, '[]'::jsonb)) AS b(id)
                        WHERE NOT EXISTS (SELECT 1 FROM gea."Benefit" r WHERE r."Id" = b.id AND r."IsActive")),
    'inheritedFrom', %(parent)s::uuid IS NULL OR EXISTS (SELECT 1 FROM gea."Project" WHERE "Id" = %(parent)s::uuid),
    'owner', %(owner)s::uuid IS NULL OR EXISTS (SELECT 1 FROM gea."User" WHERE "Id" = %(owner)s::uuid)) AS body
"""

INSERT_PROJECT = """
INSERT INTO gea."Project" ("Name", "RegionId", "BusinessPurposeId", "Description", "Period", "PeriodFrom", "PeriodTo",
                           "ParentProjectId", "OwnerId", "State", "CreatedBy", "UpdatedBy")
VALUES (%(name)s::text, %(region)s::text, %(business_purpose)s::text, %(description)s::text, %(cycle)s::text,
        %(period_from)s::date, %(period_to)s::date, %(parent)s::uuid,
        coalesce(%(owner)s::uuid, %(user)s::uuid), coalesce(%(state)s::text, 'in-progress'),
        %(user)s::uuid, %(user)s::uuid)
RETURNING "Id"::text AS id
"""

INSERT_PROJECT_BENEFITS = """
INSERT INTO gea."ProjectBenefit" ("ProjectId", "BenefitId")
SELECT %(id)s::uuid, id FROM jsonb_array_elements_text(%(benefits)s::jsonb) AS b(id)
"""

DELETE_PROJECT_BENEFITS = """DELETE FROM gea."ProjectBenefit" WHERE "ProjectId" = %(id)s::uuid"""

# Merge patch: a member is written only when its set_<name> flag is true.
UPDATE_PROJECT = """
UPDATE gea."Project" SET
    "Name"        = CASE WHEN %(set_name)s::boolean THEN %(name)s::text ELSE "Name" END,
    "Description" = CASE WHEN %(set_description)s::boolean THEN %(description)s::text ELSE "Description" END,
    "Period"      = CASE WHEN %(set_cycle)s::boolean THEN %(cycle)s::text ELSE "Period" END,
    "PeriodFrom"  = CASE WHEN %(set_period_from)s::boolean THEN %(period_from)s::date ELSE "PeriodFrom" END,
    "PeriodTo"    = CASE WHEN %(set_period_to)s::boolean THEN %(period_to)s::date ELSE "PeriodTo" END,
    "OwnerId"     = CASE WHEN %(set_owner)s::boolean THEN %(owner)s::uuid ELSE "OwnerId" END,
    "UpdatedBy"   = %(user)s::uuid
WHERE "Id" = %(id)s::uuid
"""

# ---------------------------------------------------------------------------- runs
# `body` is everything about the run except its configuration; `configuration` is the
# createRun.* props that are filled in. gea_api/runconfig.py puts the two together.
_RUN_COLUMNS = """
       jsonb_build_object(
           'id', r."Id", 'projectId', r."ProjectId", 'projectName', p."Name", 'name', r."Name", 'treaty', r."Treaty",
           'region', p."RegionId", 'status', r."Status", 'locked', r."Locked", 'cloneSourceId', r."CloneSourceId",
           'currentContract', (SELECT jsonb_build_object('contractId', k."Id", 'version', k."Version",
                                                         'contentHash', k."ContentHash"::text,
                                                         'snowflakeDelivery', coalesce(dl."Status", 'pending'))
                               FROM gea."DataContract" k
                               LEFT JOIN gea."ContractDelivery" dl ON dl."ContractId" = k."Id" AND dl."Target" = 'snowflake'
                               WHERE k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"),
           'submittedAt', r."SubmittedAt",
           'submittedBy', (SELECT jsonb_build_object('id', u."Id", 'name', u."Name")
                           FROM gea."User" u WHERE u."Id" = r."SubmittedBy"),
           'failureMessage', r."FailureMessage",
           'jobId', r."JobId", 'jobOrdinal', r."JobOrdinal", 'cancelRequestedAt', r."CancelRequestedAt",
           'resolution', CASE WHEN r."ResolutionAction" IS NOT NULL THEN jsonb_build_object(
                             'action', r."ResolutionAction", 'note', r."ResolutionNote", 'resolvedAt', r."ResolvedAt",
                             'resolvedBy', (SELECT jsonb_build_object('id', u."Id", 'name', u."Name")
                                            FROM gea."User" u WHERE u."Id" = r."ResolvedBy")) END,
           'revision', r."Revision", 'createdAt', r."CreatedAt", 'updatedAt', r."UpdatedAt") AS body,
       gea."RunConfiguration"(r."Id") AS configuration"""

GET_RUN = f"""
SELECT {_RUN_COLUMNS}
FROM gea."Run" r
JOIN gea."Project" p ON p."Id" = r."ProjectId"
WHERE r."Id" = %(id)s::uuid
"""

# FOR UPDATE: the exclusion list takes FOR SHARE on the run row in its trigger, so while
# this transaction holds the lock the whole configuration of the run stands still.
LOCK_RUN = """SELECT "Status" AS status FROM gea."Run" WHERE "Id" = %(id)s::uuid FOR UPDATE"""

LIST_RUNS = f"""
SELECT {_RUN_COLUMNS},
       r."CreatedAt"::text AS cursor_time, r."Id"::text AS cursor_id
FROM gea."Run" r
JOIN gea."Project" p ON p."Id" = r."ProjectId"
WHERE (%(project_id)s::uuid IS NULL OR r."ProjectId" = %(project_id)s::uuid)
  AND (%(job_id)s::uuid IS NULL OR r."JobId" = %(job_id)s::uuid)
  AND (%(status)s::text IS NULL OR r."Status" = %(status)s::text)
  AND (%(q)s::text IS NULL OR r."Name" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\'
                           OR r."Treaty" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\'
                           OR p."RegionId" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\'
                           OR r."Id"::text ILIKE %(q)s::text || '%%' ESCAPE '\\')
  AND (%(after_time)s::timestamptz IS NULL
       OR r."CreatedAt" < %(after_time)s::timestamptz
       OR (r."CreatedAt" = %(after_time)s::timestamptz AND r."Id" > %(after_id)s::uuid))
ORDER BY r."CreatedAt" DESC, r."Id"
LIMIT %(limit)s::int
"""

PROJECT_EXISTS = """SELECT 1 AS found FROM gea."Project" WHERE "Id" = %(id)s::uuid"""

RUN_EXISTS = """SELECT 1 AS found FROM gea."Run" WHERE "Id" = %(id)s::uuid"""

INSERT_RUN = """
INSERT INTO gea."Run" ("ProjectId", "Name", "Treaty", "CloneSourceId", "CreatedBy", "UpdatedBy")
VALUES (%(project_id)s::uuid, %(name)s::text, %(treaty)s::text, %(source)s::uuid, %(user)s::uuid, %(user)s::uuid)
RETURNING "Id"::text AS id
"""

COPY_EXCLUSIONS = """
INSERT INTO gea."RunStudyPeriodExclusion" ("RunId", "Ordinal", "StartDate", "EndDate")
SELECT %(id)s::uuid, x."Ordinal", x."StartDate", x."EndDate"
FROM gea."RunStudyPeriodExclusion" x
WHERE x."RunId" = %(source)s::uuid
"""

DELETE_EXCLUSIONS = """DELETE FROM gea."RunStudyPeriodExclusion" WHERE "RunId" = %(id)s::uuid"""

INSERT_EXCLUSIONS = """
INSERT INTO gea."RunStudyPeriodExclusion" ("RunId", "Ordinal", "StartDate", "EndDate")
SELECT %(id)s::uuid, x.ordinality, (x.value ->> 'start')::date, (x.value ->> 'end')::date
FROM jsonb_array_elements(%(ranges)s::jsonb) WITH ORDINALITY AS x(value, ordinality)
"""

DELETE_RUN = """DELETE FROM gea."Run" WHERE "Id" = %(id)s::uuid"""

# Run.ResolutionAction: "re-run with fixed config" returns the failed run to draft, "mark resolved" leaves it failed.
RESOLVE_RUN = """
UPDATE gea."Run" SET
    "Status" = CASE WHEN %(action)s::text = 'rerun-with-fixed-config' THEN 'draft' ELSE "Status" END,
    "ResolutionAction" = %(action)s::text, "ResolutionNote" = %(note)s::text,
    "ResolvedBy" = %(user)s::uuid, "ResolvedAt" = now(), "UpdatedBy" = %(user)s::uuid
WHERE "Id" = %(id)s::uuid
"""

# 'cancelled': the contract was withdrawn before it was sent. 'requested': the relay passes the request on.
CANCEL_RUN = """SELECT gea."RequestRunCancel"(%(id)s::uuid, %(user)s::uuid) AS outcome"""

# ---------------------------------------------------------------------------- review, contracts
# now() is the transaction start time, so the document and the row carry the same instant.
BUILD_CONTRACT = """
SELECT gea."BuildContractDocument"(%(run_id)s::uuid, %(contract_id)s::uuid, %(user)s::uuid, now()) AS body
"""

# The triggers verify the hash and that the document equals the saved configuration, queue the
# Snowflake delivery and move the run to 'queued', all in this transaction.
INSERT_CONTRACT = """
INSERT INTO gea."DataContract" ("Id", "RunId", "ProjectId", "Version", "SpecVersion",
                               "DocumentCanonical", "ContentHash", "CreatedBy", "CreatedAt")
VALUES (%(contract_id)s::uuid, %(run_id)s::uuid, %(project_id)s::uuid, %(version)s::int, %(spec_version)s::text,
        %(document)s::text, %(hash)s::text, %(user)s::uuid, now())
"""

_DELIVERY = """jsonb_build_object(
                   'target', d."Target", 'status', d."Status", 'attempts', d."Attempts", 'lastError', d."LastError",
                   'nextAttemptAt', CASE WHEN d."Status" = 'pending' THEN d."NextAttemptAt" END,
                   'deliveredAt', d."DeliveredAt")"""

_DELIVERIES = f"""coalesce((SELECT jsonb_agg({_DELIVERY} ORDER BY d."Target")
                           FROM gea."ContractDelivery" d WHERE d."ContractId" = k."Id"), '[]'::jsonb)"""

_CONTRACT_BODY = f"""jsonb_build_object(
    'contractId', k."Id", 'runId', k."RunId", 'projectId', k."ProjectId", 'version', k."Version",
    'specVersion', k."SpecVersion", 'contentHash', k."ContentHash"::text, 'createdAt', k."CreatedAt",
    'createdBy', jsonb_build_object('id', u."Id", 'name', u."Name"),
    'document', k."Document",
    'deliveries', {_DELIVERIES})"""

GET_CONTRACT = f"""
SELECT {_CONTRACT_BODY} AS body
FROM gea."DataContract" k
JOIN gea."User" u ON u."Id" = k."CreatedBy"
WHERE k."Id" = %(id)s::uuid
"""

GET_RUN_CONTRACT = f"""
SELECT {_CONTRACT_BODY} AS body
FROM gea."DataContract" k
JOIN gea."User" u ON u."Id" = k."CreatedBy"
WHERE k."RunId" = %(run_id)s::uuid AND k."Version" = %(version)s::int
"""

LIST_RUN_CONTRACTS = f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
           'contractId', k."Id", 'version', k."Version", 'contentHash', k."ContentHash"::text,
           'createdAt', k."CreatedAt", 'createdBy', jsonb_build_object('id', u."Id", 'name', u."Name"),
           'deliveries', {_DELIVERIES}) ORDER BY k."Version" DESC), '[]'::jsonb) AS body
FROM gea."DataContract" k
JOIN gea."User" u ON u."Id" = k."CreatedBy"
WHERE k."RunId" = %(run_id)s::uuid
"""

# Where the execution of the run's current contract is. Before the contract has reached Snowflake
# there is no gea."RunExecution" row: the status is then 'queued' (or 'failed' when the delivery was
# withdrawn) and the steps are those of the contract, all pending.
GET_RUN_EXECUTION = f"""
SELECT jsonb_build_object(
           'contractId', k."Id", 'contractVersion', k."Version",
           'status', coalesce(e."Status", CASE WHEN d."Status" = 'cancelled' THEN 'failed' ELSE 'queued' END),
           'delivery', {_DELIVERY},
           'startedAt', e."StartedAt", 'finishedAt', e."FinishedAt",
           'failureMessage', coalesce(e."FailureMessage", CASE WHEN d."Status" = 'cancelled' THEN d."LastError" END),
           'lastReportedAt', e."UpdatedAt",
           'cancelRequestedAt', r."CancelRequestedAt",
           'cancelRequestedBy', (SELECT jsonb_build_object('id', u."Id", 'name', u."Name")
                                 FROM gea."User" u WHERE u."Id" = r."CancelRequestedBy"),
           'steps', coalesce(
               (SELECT jsonb_agg(jsonb_build_object(
                           'stepKey', s."StepKey", 'ordinal', s."Ordinal", 'status', s."Status",
                           'startedAt', s."StartedAt", 'finishedAt', s."FinishedAt", 'detail', s."Detail")
                       ORDER BY s."Ordinal")
                FROM gea."RunExecutionStep" s WHERE s."ContractId" = e."ContractId"),
               (SELECT jsonb_agg(jsonb_build_object(
                           'stepKey', c ->> 'key', 'ordinal', (c ->> 'ordinal')::int,
                           'status', CASE WHEN d."Status" = 'cancelled' THEN 'skipped' ELSE 'pending' END,
                           'startedAt', NULL, 'finishedAt', NULL, 'detail', NULL)
                       ORDER BY (c ->> 'ordinal')::int)
                FROM jsonb_array_elements(k."Document" -> 'steps') c))) AS body
FROM gea."Run" r
JOIN gea."DataContract" k ON k."RunId" = r."Id" AND k."Version" = r."CurrentContractVersion"
JOIN gea."ContractDelivery" d ON d."ContractId" = k."Id" AND d."Target" = 'snowflake'
LEFT JOIN gea."RunExecution" e ON e."ContractId" = k."Id"
WHERE r."Id" = %(run_id)s::uuid
"""

# Run.Logs, newest first. The id grows with every line, so it is the cursor.
LIST_RUN_LOGS = """
SELECT jsonb_build_object('id', l."Id", 'contractVersion', k."Version", 'stepKey', l."StepKey", 'level', l."Level",
                          'message', l."Message", 'detail', l."Detail", 'occurredAt', l."OccurredAt") AS body,
       l."OccurredAt"::text AS cursor_time, l."Id"::text AS cursor_id
FROM gea."RunLog" l
JOIN gea."DataContract" k ON k."Id" = l."ContractId"
WHERE l."RunId" = %(run_id)s::uuid
  AND (%(after_id)s::bigint IS NULL OR l."Id" < %(after_id)s::bigint)
ORDER BY l."Id" DESC
LIMIT %(limit)s::int
"""

RETRY_DELIVERY = """SELECT gea."RetryContractDelivery"(%(id)s::uuid, 'snowflake') AS retried"""

GET_DELIVERY = f"""
SELECT {_DELIVERY} AS body
FROM gea."ContractDelivery" d
WHERE d."ContractId" = %(id)s::uuid AND d."Target" = 'snowflake'
"""

# ---------------------------------------------------------------------------- jobs
# Status, counters, progress and duration are derived from the runs of the job (gea."JobSummary").
_JOB_BODY = """jsonb_build_object(
    'id', j."Id", 'name', j."Name", 'kind', j."Kind", 'note', j."Note",
    'projectId', j."ProjectId", 'projectName', j."ProjectName",
    'status', j."Status", 'maxParallel', j."MaxParallel",
    'runs', jsonb_build_object('count', j."RunCount", 'completed', j."CompletedRunCount", 'failed', j."FailedRunCount",
                               'running', j."RunningRunCount", 'queued', j."QueuedRunCount"),
    'progressPercentage', coalesce(j."ProgressPercentage", 0),
    'durationSeconds', CASE WHEN j."FinishedAt" IS NOT NULL
                            THEN greatest(0, extract(epoch FROM j."FinishedAt" - j."SubmittedAt"))::int END,
    'finishedAt', j."FinishedAt",
    'submittedBy', jsonb_build_object('id', j."SubmittedBy", 'name', j."SubmittedByName"),
    'submittedAt', j."SubmittedAt")"""

GET_JOB = f"""
SELECT {_JOB_BODY} AS body
FROM gea."JobSummary" j
WHERE j."Id" = %(id)s::uuid
"""

LIST_JOBS = f"""
SELECT {_JOB_BODY} AS body, j."SubmittedAt"::text AS cursor_time, j."Id"::text AS cursor_id
FROM gea."JobSummary" j
WHERE (%(project_id)s::uuid IS NULL OR j."ProjectId" = %(project_id)s::uuid)
  AND (%(status)s::text IS NULL OR j."Status" = %(status)s::text)
  AND (%(q)s::text IS NULL OR j."Name" ILIKE '%%' || %(q)s::text || '%%' ESCAPE '\\')
  AND (%(after_time)s::timestamptz IS NULL
       OR j."SubmittedAt" < %(after_time)s::timestamptz
       OR (j."SubmittedAt" = %(after_time)s::timestamptz AND j."Id" > %(after_id)s::uuid))
ORDER BY j."SubmittedAt" DESC, j."Id"
LIMIT %(limit)s::int
"""

# Lock the runs of a new job in one fixed order, so that two jobs over the same runs cannot deadlock.
LOCK_RUNS = """
SELECT r."Id"::text AS id
FROM gea."Run" r
WHERE r."Id" IN (SELECT x::uuid FROM jsonb_array_elements_text(%(ids)s::jsonb) AS x)
ORDER BY r."Id"
FOR UPDATE
"""

# The project of a job: the one all its runs share, or none.
INSERT_JOB = """
INSERT INTO gea."Job" ("Name", "Note", "MaxParallel", "SubmittedBy", "ProjectId")
SELECT %(name)s::text, %(note)s::text, %(max_parallel)s::smallint, %(user)s::uuid,
       (SELECT CASE WHEN count(DISTINCT r."ProjectId") = 1 THEN (array_agg(r."ProjectId"))[1] END
        FROM gea."Run" r
        WHERE r."Id" IN (SELECT x::uuid FROM jsonb_array_elements_text(%(ids)s::jsonb) AS x))
RETURNING "Id"::text AS id
"""

# Allowed only while the run is a draft: the trigger treats the job of a run as configuration.
ASSIGN_JOB = """
UPDATE gea."Run" SET "JobId" = %(job_id)s::uuid, "JobOrdinal" = %(ordinal)s::smallint, "UpdatedBy" = %(user)s::uuid
WHERE "Id" = %(id)s::uuid
"""

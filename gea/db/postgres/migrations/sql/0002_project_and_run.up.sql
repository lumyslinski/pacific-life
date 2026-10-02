-- 0002 Project, Run and the values offered in the run dropdowns.
--
-- Every prop of workbook sheet 'POC Data' is a column:
--   createProject.name / region / businessPurpose   -> "Project"."Name" / "RegionId" / "BusinessPurposeId"
--   createProject.benefit (array<string>)           -> "ProjectBenefit"
--   createRun.name / treaty                         -> "Run"."Name" / "Treaty"
--   createRun.<prop>                                -> "Run"."<Prop>"
--   createRun.studyPeriod, ibnrStudyPeriod          -> two date columns, ...Start and ...End
--   createRun.studyPeriodExclusions                 -> "RunStudyPeriodExclusion"
-- gea/spec/poc-data.json lists the props with their columns; tests/gea compares the two.
--
-- Lifecycle enforced here, not only in the API:
--   * a run is created as 'draft' in a project that is not signed off;
--   * its configuration can change only while it is 'draft';
--   * it can leave 'draft' only with every required prop filled ("CK_Run_RequiredWhenSubmitted");
--   * status moves only along draft -> queued -> running -> complete | failed, failed -> draft.

-- ---------------------------------------------------------------------------
-- Project. Columns are the stored fields of '03. Model Data' (Project.*) and of
-- '04. API Data'; counters such as Project.RunCount are derived, see the view
-- "ProjectSummary" in revision 0003.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."Project" (
    "Id"                 uuid NOT NULL DEFAULT gen_random_uuid(),   -- Project.Id
    "Name"               text NOT NULL,                             -- Project.Name, createProject.name
    "BusinessPurposeId"  text NOT NULL,                             -- Project.BusinessPurpose
    "RegionId"           text NOT NULL,                             -- Project.Region
    "Period"             text,                                      -- Project.Period: the cycle, normally a year
    "PeriodFrom"         date,                                      -- projects[].periodFrom
    "PeriodTo"           date,                                      -- projects[].periodTo
    "OwnerId"            uuid NOT NULL,                             -- Project.Owner
    "State"              text NOT NULL DEFAULT 'in-progress',       -- Project.State
    "Description"        text,                                      -- Project.Description
    "ParentProjectId"    uuid,                                      -- Project.ParentProjectId, projects[].inheritedFrom
    "Locked"             boolean GENERATED ALWAYS AS ("State" = 'signed-off') STORED,   -- Project.Locked
    "SignedOffAt"        timestamptz,                               -- Project.SignedOffAt
    "SignedOffBy"        uuid,                                      -- Project.SignedOffBy
    "Revision"           integer NOT NULL DEFAULT 1,
    "CreatedBy"          uuid NOT NULL,
    "CreatedAt"          timestamptz NOT NULL DEFAULT now(),
    "UpdatedBy"          uuid NOT NULL,
    "UpdatedAt"          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_Project" PRIMARY KEY ("Id"),
    CONSTRAINT "FK_Project_BusinessPurpose" FOREIGN KEY ("BusinessPurposeId") REFERENCES gea."BusinessPurpose" ("Id"),
    CONSTRAINT "FK_Project_Region" FOREIGN KEY ("RegionId") REFERENCES gea."Region" ("Id"),
    CONSTRAINT "FK_Project_Owner" FOREIGN KEY ("OwnerId") REFERENCES gea."User" ("Id"),
    CONSTRAINT "FK_Project_ParentProject" FOREIGN KEY ("ParentProjectId") REFERENCES gea."Project" ("Id"),
    CONSTRAINT "FK_Project_SignedOffBy" FOREIGN KEY ("SignedOffBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "FK_Project_CreatedBy" FOREIGN KEY ("CreatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "FK_Project_UpdatedBy" FOREIGN KEY ("UpdatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_Project_Name" CHECK (btrim("Name") <> '' AND char_length("Name") <= 200),
    CONSTRAINT "CK_Project_Period" CHECK (char_length("Period") <= 50),
    CONSTRAINT "CK_Project_PeriodOrder" CHECK ("PeriodFrom" <= "PeriodTo"),
    CONSTRAINT "CK_Project_Description" CHECK (char_length("Description") <= 4000),
    CONSTRAINT "CK_Project_State" CHECK ("State" IN ('draft', 'in-progress', 'pending-review', 'ready', 'signed-off')),
    CONSTRAINT "CK_Project_SignedOff" CHECK (("State" = 'signed-off') = ("SignedOffAt" IS NOT NULL AND "SignedOffBy" IS NOT NULL)),
    CONSTRAINT "CK_Project_ParentProject" CHECK ("ParentProjectId" IS DISTINCT FROM "Id"),
    CONSTRAINT "CK_Project_Revision" CHECK ("Revision" >= 1)
);

CREATE INDEX "IX_Project_Filter" ON gea."Project" ("BusinessPurposeId", "State", "RegionId");
CREATE INDEX "IX_Project_OwnerId" ON gea."Project" ("OwnerId");
CREATE INDEX "IX_Project_CreatedAt" ON gea."Project" ("CreatedAt" DESC, "Id");
CREATE INDEX "IX_Project_ParentProjectId" ON gea."Project" ("ParentProjectId") WHERE "ParentProjectId" IS NOT NULL;

-- createProject.benefit is array<string> (multiselect).
CREATE TABLE gea."ProjectBenefit" (
    "ProjectId"  uuid NOT NULL,
    "BenefitId"  text NOT NULL,
    CONSTRAINT "PK_ProjectBenefit" PRIMARY KEY ("ProjectId", "BenefitId"),
    CONSTRAINT "FK_ProjectBenefit_Project" FOREIGN KEY ("ProjectId") REFERENCES gea."Project" ("Id") ON DELETE CASCADE,
    CONSTRAINT "FK_ProjectBenefit_Benefit" FOREIGN KEY ("BenefitId") REFERENCES gea."Benefit" ("Id")
);

CREATE INDEX "IX_ProjectBenefit_BenefitId" ON gea."ProjectBenefit" ("BenefitId");

CREATE FUNCTION gea."Project_BeforeUpdate"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD."Locked" AND NEW."State" = 'signed-off' THEN
        RAISE EXCEPTION 'project "%" is signed off and locked', OLD."Name"
            USING ERRCODE = 'GEA03';
    END IF;
    NEW."Revision" := OLD."Revision" + 1;
    NEW."UpdatedAt" := now();
    RETURN NEW;
END $$;

CREATE TRIGGER "Project_BeforeUpdate"
    BEFORE UPDATE ON gea."Project"
    FOR EACH ROW EXECUTE FUNCTION gea."Project_BeforeUpdate"();

-- At least one benefit per project (createProject.benefit is required), checked
-- at commit so that the project and its benefits can be written in any order.
CREATE FUNCTION gea."Project_RequiresBenefit"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_project_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'Project' THEN
        v_project_id := NEW."Id";
    ELSE
        v_project_id := OLD."ProjectId";
    END IF;
    IF EXISTS (SELECT 1 FROM gea."Project" WHERE "Id" = v_project_id)
       AND NOT EXISTS (SELECT 1 FROM gea."ProjectBenefit" WHERE "ProjectId" = v_project_id) THEN
        RAISE EXCEPTION 'project % needs at least one benefit', v_project_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER "Project_RequiresBenefit"
    AFTER INSERT ON gea."Project"
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION gea."Project_RequiresBenefit"();

CREATE CONSTRAINT TRIGGER "ProjectBenefit_RequiresOne"
    AFTER DELETE ON gea."ProjectBenefit"
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION gea."Project_RequiresBenefit"();

-- ---------------------------------------------------------------------------
-- Run. One typed column per createRun.* prop, in workbook order. A draft may
-- have any of them empty; "CK_Run_RequiredWhenSubmitted" lists the props the
-- workbook marks as required (column F).
-- ---------------------------------------------------------------------------
CREATE TABLE gea."Run" (
    "Id"                            uuid NOT NULL DEFAULT gen_random_uuid(),   -- Run.Id
    "ProjectId"                     uuid NOT NULL,                             -- Run.ProjectId
    "Name"                          text NOT NULL,                             -- createRun.name (required)
    "Treaty"                        text NOT NULL,                             -- createRun.treaty (required)
    -- step dataAndSetUp
    "DataScope"                     text[],     -- createRun.dataScope (required)
    "StudyPeriodStart"              date,       -- createRun.studyPeriod (required)
    "StudyPeriodEnd"                date,       -- createRun.studyPeriod (required)
    -- createRun.studyPeriodExclusions (optional): table "RunStudyPeriodExclusion"
    "StudyPeriodTreatyOverride"     boolean,    -- createRun.studyPeriodTreatyOverride (required)
    "PerTreatyEndDatesMapping"      text,       -- createRun.perTreatyEndDatesMapping (optional)
    "Investigation"                 text,       -- createRun.investigation (required)
    -- step segmentation
    "ExposureMethod"                text,       -- createRun.exposureMethod (required)
    "InitialExposureMethod"         text,       -- createRun.initialExposureMethod (required)
    "ExposureExclusion"             text[],     -- createRun.exposureExclusion (optional)
    "PolicyTenureSegmentation"      text,       -- createRun.policyTenureSegmentation (optional)
    "CalendarTenureSegmentation"    text,       -- createRun.calendarTenureSegmentation (optional)
    "AttainedAgeSegmentation"       text,       -- createRun.attainedAgeSegmentation (optional)
    -- step actuals
    "PartialClaimTreatment"         text,       -- createRun.partialClaimTreatment (required)
    "AmountBasis"                   text,       -- createRun.amountBasis (required)
    -- step ibnr
    "ClaimBasis"                    text,       -- createRun.claimBasis (required)
    "IbnrMethodology"               text,       -- createRun.ibnrMethodology (required)
    "DerivationMethod"              text,       -- createRun.derivationMethod (required)
    "IbnrStudyPeriodStart"          date,       -- createRun.ibnrStudyPeriod (required)
    "IbnrStudyPeriodEnd"            date,       -- createRun.ibnrStudyPeriod (required)
    "DevelopmentFrequency"          text,       -- createRun.developmentFrequency (required)
    "UpliftFrequency"               text,       -- createRun.upliftFrequency (required)
    "EventMonthFilter"              boolean,    -- createRun.eventMonthFilter (optional)
    "ReportingMonthFilter"          boolean,    -- createRun.reportingMonthFilter (optional)
    "IbnrRbnsBasis"                 text,       -- createRun.ibnrRbnsBasis (required)
    "TailStartPeriod"               text,       -- createRun.tailStartPeriod (optional)
    -- step assigningExpected
    "ComparisonBases"               text,       -- createRun.comparisonBases (optional)
    "TrendAssumptions"              text,       -- createRun.trendAssumptions (optional)
    -- step ultimateCalculation
    "Adjustment"                    text,       -- createRun.adjustment (required)
    "UltimateRbnsBasis"             text,       -- createRun.ultimateRbnsBasis (required)
    -- step actualExpected
    "OutputFrequency"               text[],     -- createRun.outputFrequency (required)
    "AdditionalOutputFields"        text[],     -- createRun.additionalOutputFields (optional)
    -- lifecycle ('03. Model Data', Run.*)
    "Status"                        text NOT NULL DEFAULT 'draft',             -- Run.Status
    "Locked"                        boolean GENERATED ALWAYS AS ("Status" <> 'draft') STORED,   -- Run.Locked
    "CloneSourceId"                 uuid,                                      -- Run.CloneSourceId
    "CurrentContractVersion"        integer,                                   -- foreign key added in 0003
    "SubmittedAt"                   timestamptz,                               -- Run.SubmittedAt
    "SubmittedBy"                   uuid,                                      -- Run.SubmittedBy
    "FailureMessage"                text,                                      -- Run.FailureMessage
    "Revision"                      integer NOT NULL DEFAULT 1,
    "CreatedBy"                     uuid NOT NULL,
    "CreatedAt"                     timestamptz NOT NULL DEFAULT now(),        -- Run.Created
    "UpdatedBy"                     uuid NOT NULL,
    "UpdatedAt"                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "PK_Run" PRIMARY KEY ("Id"),
    CONSTRAINT "UQ_Run_Id_ProjectId" UNIQUE ("Id", "ProjectId"),       -- target of the contract's composite foreign key
    CONSTRAINT "FK_Run_Project" FOREIGN KEY ("ProjectId") REFERENCES gea."Project" ("Id"),
    CONSTRAINT "FK_Run_CloneSource" FOREIGN KEY ("CloneSourceId") REFERENCES gea."Run" ("Id"),
    CONSTRAINT "FK_Run_SubmittedBy" FOREIGN KEY ("SubmittedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "FK_Run_CreatedBy" FOREIGN KEY ("CreatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "FK_Run_UpdatedBy" FOREIGN KEY ("UpdatedBy") REFERENCES gea."User" ("Id"),
    CONSTRAINT "CK_Run_Name" CHECK (btrim("Name") <> '' AND char_length("Name") <= 200),
    CONSTRAINT "CK_Run_Treaty" CHECK (btrim("Treaty") <> ''),
    CONSTRAINT "CK_Run_Status" CHECK ("Status" IN ('draft', 'queued', 'running', 'complete', 'failed')),
    CONSTRAINT "CK_Run_CloneSource" CHECK ("CloneSourceId" IS DISTINCT FROM "Id"),
    CONSTRAINT "CK_Run_Revision" CHECK ("Revision" >= 1),
    CONSTRAINT "CK_Run_ContractWhenSubmitted" CHECK ("Status" = 'draft' OR "CurrentContractVersion" IS NOT NULL),
    CONSTRAINT "CK_Run_StudyPeriod" CHECK (("StudyPeriodStart" IS NULL) = ("StudyPeriodEnd" IS NULL) AND "StudyPeriodStart" <= "StudyPeriodEnd"),
    CONSTRAINT "CK_Run_IbnrStudyPeriod" CHECK (("IbnrStudyPeriodStart" IS NULL) = ("IbnrStudyPeriodEnd" IS NULL) AND "IbnrStudyPeriodStart" <= "IbnrStudyPeriodEnd"),
    -- The required props of sheet 'POC Data'. The last line is the one condition
    -- the modelling team describes: a treaty override needs its end-date mapping.
    CONSTRAINT "CK_Run_RequiredWhenSubmitted" CHECK ("Status" = 'draft' OR (
            coalesce(cardinality("DataScope"), 0) > 0
            AND "StudyPeriodStart" IS NOT NULL AND "StudyPeriodEnd" IS NOT NULL
            AND "StudyPeriodTreatyOverride" IS NOT NULL
            AND "Investigation" IS NOT NULL
            AND "ExposureMethod" IS NOT NULL
            AND "InitialExposureMethod" IS NOT NULL
            AND "PartialClaimTreatment" IS NOT NULL
            AND "AmountBasis" IS NOT NULL
            AND "ClaimBasis" IS NOT NULL
            AND "IbnrMethodology" IS NOT NULL
            AND "DerivationMethod" IS NOT NULL
            AND "IbnrStudyPeriodStart" IS NOT NULL AND "IbnrStudyPeriodEnd" IS NOT NULL
            AND "DevelopmentFrequency" IS NOT NULL
            AND "UpliftFrequency" IS NOT NULL
            AND "IbnrRbnsBasis" IS NOT NULL
            AND "Adjustment" IS NOT NULL
            AND "UltimateRbnsBasis" IS NOT NULL
            AND coalesce(cardinality("OutputFrequency"), 0) > 0
            AND ("StudyPeriodTreatyOverride" IS NOT TRUE OR "PerTreatyEndDatesMapping" IS NOT NULL)))
);

CREATE INDEX "IX_Run_ProjectId" ON gea."Run" ("ProjectId", "CreatedAt" DESC, "Id");
CREATE INDEX "IX_Run_Status" ON gea."Run" ("Status");
CREATE INDEX "IX_Run_Treaty" ON gea."Run" ("Treaty");
CREATE INDEX "IX_Run_CloneSourceId" ON gea."Run" ("CloneSourceId") WHERE "CloneSourceId" IS NOT NULL;

-- createRun.studyPeriodExclusions is array<{start: date, end: date}>.
CREATE TABLE gea."RunStudyPeriodExclusion" (
    "RunId"      uuid NOT NULL,
    "Ordinal"    smallint NOT NULL,          -- position in the list, from 1
    "StartDate"  date NOT NULL,
    "EndDate"    date NOT NULL,
    CONSTRAINT "PK_RunStudyPeriodExclusion" PRIMARY KEY ("RunId", "Ordinal"),
    CONSTRAINT "FK_RunStudyPeriodExclusion_Run" FOREIGN KEY ("RunId") REFERENCES gea."Run" ("Id") ON DELETE CASCADE,
    CONSTRAINT "CK_RunStudyPeriodExclusion_Ordinal" CHECK ("Ordinal" >= 1),
    CONSTRAINT "CK_RunStudyPeriodExclusion_Order" CHECK ("StartDate" <= "EndDate")
);

CREATE FUNCTION gea."Run_BeforeInsert"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW."Status" <> 'draft' OR NEW."CurrentContractVersion" IS NOT NULL THEN
        RAISE EXCEPTION 'a run is created as a draft without a contract'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Share-lock the project so that it cannot be signed off concurrently.
    IF (SELECT "Locked" FROM gea."Project" WHERE "Id" = NEW."ProjectId" FOR SHARE) THEN
        RAISE EXCEPTION 'the project is signed off and locked'
            USING ERRCODE = 'GEA03';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER "Run_BeforeInsert"
    BEFORE INSERT ON gea."Run"
    FOR EACH ROW EXECUTE FUNCTION gea."Run_BeforeInsert"();

CREATE FUNCTION gea."Run_BeforeUpdate"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- Everything except these columns is the configuration of the run.
    c_lifecycle CONSTANT text[] := ARRAY['Status', 'Locked', 'CurrentContractVersion', 'SubmittedAt', 'SubmittedBy',
                                         'FailureMessage', 'Revision', 'UpdatedBy', 'UpdatedAt'];
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

    NEW."Revision" := OLD."Revision" + 1;
    NEW."UpdatedAt" := now();
    RETURN NEW;
END $$;

CREATE TRIGGER "Run_BeforeUpdate"
    BEFORE UPDATE ON gea."Run"
    FOR EACH ROW EXECUTE FUNCTION gea."Run_BeforeUpdate"();

CREATE FUNCTION gea."Run_BeforeDelete"() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD."Status" <> 'draft' OR OLD."CurrentContractVersion" IS NOT NULL THEN
        RAISE EXCEPTION 'run "%" has been submitted and cannot be deleted', OLD."Name"
            USING ERRCODE = 'GEA03';
    END IF;
    RETURN OLD;
END $$;

CREATE TRIGGER "Run_BeforeDelete"
    BEFORE DELETE ON gea."Run"
    FOR EACH ROW EXECUTE FUNCTION gea."Run_BeforeDelete"();

-- The exclusions are part of the configuration: writable only while the run is a draft.
CREATE FUNCTION gea."RunStudyPeriodExclusion_BeforeWrite"() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_run_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD."RunId" ELSE NEW."RunId" END;
    v_status text;
BEGIN
    -- Share-lock the run: publishing a contract takes FOR UPDATE on the same row,
    -- so the list cannot change underneath a contract that is being frozen.
    -- No row means the draft run itself is being deleted (cascade): allowed.
    SELECT "Status" INTO v_status FROM gea."Run" WHERE "Id" = v_run_id FOR SHARE;
    IF FOUND AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'the run is % and its configuration is locked; clone it instead', v_status
            USING ERRCODE = 'GEA03';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

CREATE TRIGGER "RunStudyPeriodExclusion_BeforeWrite"
    BEFORE INSERT OR UPDATE OR DELETE ON gea."RunStudyPeriodExclusion"
    FOR EACH ROW EXECUTE FUNCTION gea."RunStudyPeriodExclusion_BeforeWrite"();

-- ---------------------------------------------------------------------------
-- The configuration of a run as the ordered steps of sheet 'POC Data' (column C,
-- "Section / Step"), with the workbook's prop names. Empty props are left out.
-- This is what is frozen into a data contract and what the API returns.
-- ---------------------------------------------------------------------------
CREATE FUNCTION gea."RunSteps"(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_array(
        jsonb_build_object('key', 'dataAndSetUp', 'ordinal', 1, 'config', jsonb_strip_nulls(jsonb_build_object(
            'dataScope', to_jsonb(r."DataScope"),
            'studyPeriod', CASE WHEN r."StudyPeriodStart" IS NOT NULL THEN jsonb_build_object('start', r."StudyPeriodStart", 'end', r."StudyPeriodEnd") END,
            'studyPeriodExclusions', (SELECT jsonb_agg(jsonb_build_object('start', x."StartDate", 'end', x."EndDate") ORDER BY x."Ordinal")
                                      FROM gea."RunStudyPeriodExclusion" x WHERE x."RunId" = r."Id"),
            'studyPeriodTreatyOverride', r."StudyPeriodTreatyOverride",
            'perTreatyEndDatesMapping', r."PerTreatyEndDatesMapping",
            'investigation', r."Investigation"))),
        jsonb_build_object('key', 'segmentation', 'ordinal', 2, 'config', jsonb_strip_nulls(jsonb_build_object(
            'exposureMethod', r."ExposureMethod",
            'initialExposureMethod', r."InitialExposureMethod",
            'exposureExclusion', to_jsonb(r."ExposureExclusion"),
            'policyTenureSegmentation', r."PolicyTenureSegmentation",
            'calendarTenureSegmentation', r."CalendarTenureSegmentation",
            'attainedAgeSegmentation', r."AttainedAgeSegmentation"))),
        jsonb_build_object('key', 'actuals', 'ordinal', 3, 'config', jsonb_strip_nulls(jsonb_build_object(
            'partialClaimTreatment', r."PartialClaimTreatment",
            'amountBasis', r."AmountBasis"))),
        jsonb_build_object('key', 'ibnr', 'ordinal', 4, 'config', jsonb_strip_nulls(jsonb_build_object(
            'claimBasis', r."ClaimBasis",
            'ibnrMethodology', r."IbnrMethodology",
            'derivationMethod', r."DerivationMethod",
            'ibnrStudyPeriod', CASE WHEN r."IbnrStudyPeriodStart" IS NOT NULL THEN jsonb_build_object('start', r."IbnrStudyPeriodStart", 'end', r."IbnrStudyPeriodEnd") END,
            'developmentFrequency', r."DevelopmentFrequency",
            'upliftFrequency', r."UpliftFrequency",
            'eventMonthFilter', r."EventMonthFilter",
            'reportingMonthFilter', r."ReportingMonthFilter",
            'ibnrRbnsBasis', r."IbnrRbnsBasis",
            'tailStartPeriod', r."TailStartPeriod"))),
        jsonb_build_object('key', 'assigningExpected', 'ordinal', 5, 'config', jsonb_strip_nulls(jsonb_build_object(
            'comparisonBases', r."ComparisonBases",
            'trendAssumptions', r."TrendAssumptions"))),
        jsonb_build_object('key', 'ultimateCalculation', 'ordinal', 6, 'config', jsonb_strip_nulls(jsonb_build_object(
            'adjustment', r."Adjustment",
            'ultimateRbnsBasis', r."UltimateRbnsBasis"))),
        jsonb_build_object('key', 'actualExpected', 'ordinal', 7, 'config', jsonb_strip_nulls(jsonb_build_object(
            'outputFrequency', to_jsonb(r."OutputFrequency"),
            'additionalOutputFields', to_jsonb(r."AdditionalOutputFields")))))
    FROM gea."Run" r
    WHERE r."Id" = p_run_id
$$;

-- The same props as one flat object: { "dataScope": [...], "studyPeriod": {...}, ... }.
CREATE FUNCTION gea."RunConfiguration"(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_object_agg(c.key, c.value), '{}'::jsonb)
    FROM jsonb_array_elements(gea."RunSteps"(p_run_id)) AS s(step)
    CROSS JOIN LATERAL jsonb_each(s.step -> 'config') AS c(key, value)
$$;

-- ---------------------------------------------------------------------------
-- ParameterOption: the values offered in the dropdown of a prop ("Parameter" is
-- the prop name of sheet 'POC Data'). Sheet 'Draft - Modelling team' says the
-- lists depend on Region / Business Purpose / Benefit / Investigation, so a row
-- can be limited to one of each; an empty scope column means "any".
-- When exactly one value applies to a scope, the prop is a pre-populated default.
-- The values below are the examples of that sheet. They are offered, not enforced:
-- "Run" stores whatever value was sent, because the real lists are still open.
-- ---------------------------------------------------------------------------
CREATE TABLE gea."ParameterOption" (
    "Id"                 integer GENERATED ALWAYS AS IDENTITY,
    "Parameter"          text NOT NULL,
    "Value"              text NOT NULL,
    "Label"              text NOT NULL,
    "SortOrder"          smallint NOT NULL DEFAULT 1,
    "IsDefault"          boolean NOT NULL DEFAULT false,
    "RegionId"           text,
    "BusinessPurposeId"  text,
    "BenefitId"          text,
    "Investigation"      text,
    "IsActive"           boolean NOT NULL DEFAULT true,
    CONSTRAINT "PK_ParameterOption" PRIMARY KEY ("Id"),
    CONSTRAINT "FK_ParameterOption_Region" FOREIGN KEY ("RegionId") REFERENCES gea."Region" ("Id"),
    CONSTRAINT "FK_ParameterOption_BusinessPurpose" FOREIGN KEY ("BusinessPurposeId") REFERENCES gea."BusinessPurpose" ("Id"),
    CONSTRAINT "FK_ParameterOption_Benefit" FOREIGN KEY ("BenefitId") REFERENCES gea."Benefit" ("Id"),
    CONSTRAINT "CK_ParameterOption_Parameter" CHECK ("Parameter" ~ '^[a-z][A-Za-z0-9]*$'),
    CONSTRAINT "CK_ParameterOption_Value" CHECK (btrim("Value") <> '')
);

CREATE UNIQUE INDEX "UQ_ParameterOption_Scope" ON gea."ParameterOption"
    ("Parameter", "Value", coalesce("RegionId", ''), coalesce("BusinessPurposeId", ''),
     coalesce("BenefitId", ''), coalesce("Investigation", ''));

INSERT INTO gea."ParameterOption" ("Parameter", "Value", "Label", "SortOrder") VALUES
    ('dataScope', 'Policy_v1', 'Policy_v1', 1),
    ('dataScope', 'Claims_v1', 'Claims_v1', 2),
    ('investigation', 'mortality', 'Mortality', 1),
    ('exposureMethod', 'initial', 'Initial', 1),
    ('initialExposureMethod', 'advance-to-next-birthday', 'Advance to Next Birthday', 1),
    ('policyTenureSegmentation', 'policy-year', 'Policy Year', 1),
    ('calendarTenureSegmentation', 'calendar-year', 'Calendar Year', 1),
    ('attainedAgeSegmentation', 'holder-age', 'Holder Age', 1),
    ('partialClaimTreatment', 'record-as-partial-claim', 'Record as partial claim', 1),
    ('amountBasis', 'lives-and-amounts', 'Lives and amounts', 1),
    ('claimBasis', 'ibnr', 'IBNR', 1),
    ('ibnrMethodology', 'chain-ladder', 'Chain Ladder', 1),
    ('derivationMethod', 'from-data', 'From data', 1),
    ('developmentFrequency', 'monthly', 'Monthly', 1),
    ('developmentFrequency', 'annual', 'Annual', 2),
    ('upliftFrequency', 'monthly', 'Monthly', 1),
    ('upliftFrequency', 'annual', 'Annual', 2),
    ('ibnrRbnsBasis', 'amounts-and-counts', 'Amounts and counts', 1),
    ('tailStartPeriod', '24-months', '24 months', 1),
    ('adjustment', 'ibnr', 'IBNR', 1),
    ('adjustment', 'rbns', 'RBNS', 2),
    ('ultimateRbnsBasis', 'amounts-and-counts', 'Amounts and counts', 1),
    ('outputFrequency', 'monthly', 'Monthly', 1),
    ('outputFrequency', 'annual', 'Annual', 2),
    ('additionalOutputFields', 'PolicyYear', 'PolicyYear', 1),
    ('additionalOutputFields', 'IssueAge', 'IssueAge', 2);

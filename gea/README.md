# GEA foundation: PostgreSQL schema, API and Vue client

Starting point for PL Re GEA Modules 2 and 3, built from
`PLRe_GEA_UX_UI_Functional_Specification_v5.xlsx` and an audit of this repository.
The API handlers are in [`../gea_api`](../gea_api), their tests in [`../tests/gea`](../tests/gea),
the diagrams on pages 13 to 17 of [`../architecture`](../architecture/README.md).

```text
User creates a Project            POST  /projects           name, region, businessPurpose, benefit
  -> creates a Run (the draft)    POST  /runs               projectId, name, treaty
  -> fills the wizard             PATCH /runs/{id}          the fields that changed; every field is a column of "Run"
  -> submits                      POST  /runs/{id}/submit   one run, or POST /jobs for several in a given order
       PostgreSQL: immutable "DataContract" row + pending Snowflake delivery, one transaction, run locked
       Snowflake : the identical document (same id, same SHA-256), delivered by an idempotent MERGE
  -> Snowflake executes the steps in order and records every step; status and log flow back to PostgreSQL
  -> the user watches, cancels, or resolves a failure   GET /runs/{id}/execution, POST .../cancel, .../resolution
```

Every change above needs a role that may prepare; a Viewer reads and watches (decision 21).

| Document | Owns |
|---|---|
| this `README.md` | Audit findings, decisions, open questions, how to verify |
| [spec/poc-data.json](spec/poc-data.json) | The 36 fields of workbook sheet "POC Data": the one list everything is checked against |
| [spec/fields.md](spec/fields.md) | Every workbook field next to the column that stores it (generated) |
| [EXECUTION.md](EXECUTION.md) | How a run executes: Snowflake computes, PostgreSQL schedules, Python relays; state, order, parallelism, cancel |
| [db/postgres/README.md](db/postgres/README.md) | PostgreSQL schema, Alembic migrations and the migration plan |
| [api/README.md](api/README.md) | Routes, the one answer shape, wizard saving, error mapping |
| [frontend/README.md](frontend/README.md) | Vue client: `ApiResult`, the wizard composable |
| [spec/data-contract.schema.json](spec/data-contract.schema.json) | Strict JSON Schema of the published contract (generated) |
| [api/openapi.yaml](api/openapi.yaml) | OpenAPI 3.1 contract (generated from `openapi.base.yaml` + the field list) |

## Layout

```text
gea/
  spec/poc-data.json                  sheet "POC Data": 4 createProject and 32 createRun props, 8 steps
  spec/fields.md                      generated: workbook row -> table and column
  spec/data-contract.schema.json      generated
  spec/data-contract.example.json     built by PostgreSQL from a saved run, validated against the schema
  tools/generate.py                   field list -> gea_api/fields.py, OpenAPI, JSON Schema, TypeScript, fields.md (--check for CI)
  tools/check_openapi.py              structure and API conventions of openapi.yaml, no network needed
  tools/erd.py                        entity-relationship diagram from the live catalogue (psql + Graphviz)
  tools/lucid_export.py               the schema as a Lucidchart ERD import file, from the live catalogue (psql)
  db/postgres/alembic.ini             Alembic configuration
  db/postgres/migrations/             env.py, gea_sql.py, versions/0001..0007, sql/<revision>.up.sql / .down.sql
  db/postgres/erd.svg                 the schema as a picture (generated)
  db/postgres/lucid-erd-import.csv    the schema for Lucidchart's ERD import (generated; how to import: db/postgres/README.md)
  db/postgres/local/init-roles.sql    roles for the local docker compose database
  db/postgres/tests/                  smoke.sql + run.sh
  db/snowflake/V001__contract_landing.sql   draft, not executed: contract landing, status and log tables
  db/snowflake/V002__run_pipeline.sql       draft, not executed: step registry, the pipeline as a task graph, step placeholders
  api/openapi.base.yaml               hand-written routes and schemas
  api/openapi.yaml                    generated: 27 paths, 35 operations, 71 schemas
  frontend/src/api/                   generated.ts, fields.ts (generated); client.ts, gea.ts (hand-written)
  frontend/src/composables/           useProjects.ts, useRunWizard.ts, useJobs.ts, useCurrentUser.ts
gea_api/                              Litestar handlers: routes, services (with the permission each operation needs), auth, queries, runconfig, errors (see api/README.md)
tests/gea/                            unit tests, migration checks, API tests against PostgreSQL
```

The field list is written once, in `spec/poc-data.json`, exactly as the workbook has it (prop, display name,
step, type, control, required). The database columns are hand-written SQL in the Alembic revisions;
`tests/gea/test_migration_sql.py` fails when a prop has no column of the right type. The API schemas, the
contract schema and the TypeScript types are generated from the same file.

To add or change a run field: edit `spec/poc-data.json`, add an Alembic revision that adds the column (and
extends `CK_Run_RequiredWhenSubmitted` and `gea."RunSteps"` when needed), run `python gea/tools/generate.py`,
commit everything it rewrites.

## Decisions taken

Decisions 2 to 5 replace the first design (snake_case tables, step payloads as JSONB validated by a
catalogue of step, field and rule tables). It was rejected in review on 2 October 2026: the fields of the
workbook have to be visible as columns. Decisions 15, 19 and 20 followed a second review the same day:
routes named after what the business calls things, and no long calculation in Python. Decision 21 (roles)
followed a third.

| # | Decision | Why |
|---|---|---|
| 1 | The field set is sheet **"POC Data"**: 4 `createProject.*` and 32 `createRun.*` props in 8 steps (main, dataAndSetUp, segmentation, actuals, ibnr, assigningExpected, ultimateCalculation, actualExpected). | It is the only sheet with a "Section / Step" column and prop names, and it matches "Draft - Modelling team". The wizard in sheets 02/03 (Dataset and Exposure, Study and Analysis, Prior Basis, Enrichment) is a different, still-Draft field set. |
| 2 | **Names come from the workbook, in PascalCase**: tables `Project`, `Run`, `User`, `Region` ..., columns `BusinessPurposeId`, `StudyPeriodStart`, `IbnrMethodology` ... Constraints are `PK_`, `FK_`, `UQ_`, `CK_`, indexes `IX_`. | Sheet 03 names the model that way (`Project.BusinessPurpose`, `Run.Treaty`), so a column can be found from the workbook without a mapping. PostgreSQL folds unquoted names to lower case, so every identifier is written in double quotes: `gea."Run"."StudyPeriodStart"`. |
| 3 | **One `Run` table with one typed column per `createRun` prop** (`text`, `boolean`, `date`, `text[]`). A date range is two columns; the list of excluded periods is the child table `RunStudyPeriodExclusion`. The eight steps are only how the wizard groups the columns. | The configuration can be read, filtered and reported with plain SQL, and the database knows the type of every field. A new field is an `ALTER TABLE` in a new revision. |
| 4 | **No field, step or rule catalogue in the database.** What a field is called, its type, step and whether it is required is the workbook's, kept in `spec/poc-data.json`; required props are one CHECK on `Run` that applies once the run is submitted. | The catalogue described columns that did not exist. With real columns it has nothing left to describe. |
| 5 | **Dropdown values are rows of one table, `ParameterOption`** (`Parameter`, `Value`, `Label`, `IsDefault`, optional `RegionId` / `BusinessPurposeId` / `BenefitId` / `Investigation`). Region, business purpose and benefit are lookup tables with foreign keys. | The workbook gives one example value per select and says the lists depend on region, business purpose, benefit and investigation (W8). A value can be added or scoped without DDL. |
| 6 | **A user has a home region**: `User.RegionId`, required, foreign key to `Region`. It is the region preselected when the user creates a project (`GET /users/me`, changed with `PATCH /users/me`). It does not restrict what the user can see. | Asked for in review. Access rules per region are not specified anywhere in the workbook (open question 7). |
| 7 | **PostgreSQL is the system of record**; Snowflake receives a copy through an outbox (`ContractDelivery`). | "In parallel" as two independent writes can leave a contract in Snowflake that PostgreSQL never committed (or the reverse). Committing the contract and its pending delivery together, then delivering idempotently, gives the same user-visible timing without that failure mode. |
| 8 | The contract is **insert-only and hash-checked in the database**: `CHECK ("ContentHash" = sha256("DocumentCanonical"))`, triggers reject UPDATE, DELETE and TRUNCATE, and the insert is refused unless the document equals what `gea."BuildContractDocument"` builds from the run's columns. | Immutability should not depend on every caller behaving. |
| 9 | Contract **version is per run** (1, 2, 3 ...). A submitted run is locked; a failed run can be reopened and resubmitted as the next version; any run can be cloned. | Matches "locked Runs cannot be edited; users create a Draft Clone" and "Re-run with fixed configuration" in the workbook. Earlier versions are never touched. |
| 10 | The API is mounted under **`/gea/v1`**. | `/runs` and `/health` already exist (with different meaning) in `api/openapi.yaml` and `api/openapi-v3-draft.yaml`. |
| 11 | Canonical JSON + SHA-256 (`calculation_api/models.py`) and the outbox are reused from the calculation API. Its body conventions (`requestId`, `expectedRevision`, `ApiError`) are **not**: see 13 and 14. | The hash must mean the same in both modules. |
| 12 | JSON names are the workbook prop names (`businessPurpose`, `benefit`, `dataScope`, `ibnrRbnsBasis` ...); option values are kebab-case (`north-america`, `chain-ladder`). | The frontend and the modelling team already use them. |
| 13 | **One answer shape: the envelope `{ data, error }` on every response**, with the real HTTP status. Success is `{ data: <resource>, error: null }`; every failure is `{ data: null, error: { code, message, status, issues, requestId } }`, with one issue per field. The Vue client unwraps it into one `ApiResult` and never throws. | Every caller reads the same two members; validation, conflicts and server errors all set a message it can show. |
| 14 | **HTTP-native idempotency and concurrency**: `Idempotency-Key` header on creating POSTs; `ETag` on every single resource and `If-Match` on every change (412 stale, 428 missing); `If-None-Match` for polling. The ETag is a hash of the representation. | Standard headers that proxies, tools and other clients understand. The ETag sent with Submit proves that nothing changed since the review. |
| 15 | **Routes are named after what the business calls things**, starting from sheet "04. API Data": `/projects`, `/runs`, `/runs/{id}`, `/datasets`, `/jobs`, `/treaties`, `/regions`, `/business-purposes`, `/benefits`. The sheet's `/profile` is `GET /projects/{id}/run-parameters` (it lists the run parameters; "profile" read as the user's profile). What the user does is the route: `POST /runs/{id}/submit`, `/cancel`, `/resolution`. A run is created with `POST /runs` and **the wizard is saved with `PATCH /runs/{id}`**: a JSON Merge Patch of the fields that changed, debounced, one save in flight. An unfinished run is stored, never rejected. What only operators need is under `/operations`. | A route should tell a business reader what it is for. One resource, one ETag, one save call for the whole wizard; nothing is lost when the browser closes. The four lookups of the sheet 02/03 wizard (`/priorbasis`, `/claimmethods`, `/actuals`, `/enrichments`) are left out (W10). |
| 16 | **Alembic, SQL-first.** Revisions run hand-written `.up.sql` / `.down.sql`; no ORM models, no autogenerate. The version table is `gea."AlembicVersion"`. | Most rules of this schema are triggers, functions, CHECKs and views, which autogenerate cannot see. Alembic adds ordering, a version table, downgrades and `--sql` output. |
| 17 | The GEA API is its **own process** (`python -m gea_api`, port 8010), separate from the calculation API. | Different store, driver and concurrency model; the calculation API serialises writers with a process lock (C6). |
| 18 | **PostgreSQL is the only store next to Snowflake; DynamoDB is removed.** The calculation module's audit exporter copies committed events to `calc."AuditEvent"` (revision `0005`): one row per event id, canonical text with SHA-256, append-only, written by the role `calc_audit`. | One database to operate. Snowflake stays authoritative for those events. |
| 19 | **Snowflake executes; PostgreSQL decides when; Python only carries messages.** Every step is a stored procedure in Snowflake (SQL, or Snowpark Python where SQL cannot express it), the order of the steps is a Snowflake task graph, and each step writes a status row before and after it runs. PostgreSQL hands contracts to the relay in the order and with the parallelism their job allows. No Python process computes or waits for a run. Details in [EXECUTION.md](EXECUTION.md). | A long calculation inside a Python process cannot be observed, is lost with the process and competes for one machine. State as rows, written where the work is done, can be watched, resumed and audited; the data stays where it is. |
| 20 | **A Job is a batch.** A run submitted on its own has no job (`Run.JobId` is empty); `POST /jobs` submits at least two runs together, all or none, in the order given and `maxParallel` at a time. Job status, counters, progress and duration are derived from its runs. | The workbook leaves open whether a run needs a job ("05. General Questions"). A job for a single run would add a row that says nothing; for a batch it is where order, parallelism and progress belong. |
| 21 | **A user has one role, and a role is three permissions.** `User.RoleId` references the lookup table `Role` (Viewer, Preparer, Reviewer, Admin), the same way `User.RegionId` references `Region`. What a role allows is three columns of its row: `CanPrepare` (create and change projects and runs, submit, cancel, resolve, clone), `CanReview` (sign off) and `CanAdminister` (users, operations). The API asks for a permission, never for a role: an operation that needs one says so in the contract (`x-permission`) and answers 403 `forbidden` without it; `GET /users/me` returns the caller's permissions for the forms. A new user is a Viewer until an Admin raises the role. | The workbook names one role ("the current user is not a Viewer") and asks what a collaborator may do (W15); the four roles were decided on 2 October 2026. Permissions as data mean that a fifth role, or a change to what a role allows, is a row and not a release, and that no form or handler compares role names. |

Two different "versions" exist and are kept apart: `specVersion` (format of the contract document, `1.0`) and
`version` (contract version within a run).

## Audit: workbook

| # | Finding | Handling here |
|---|---|---|
| W1 | Three definitions of the run configuration: "POC Data" (32 fields, 8 sections), "Draft - Modelling team" (same sections, more notes), and sheets 02/03 (`Run.DatasetSource`, `Run.MethodologyProfile`, `Run.PriorBasis*`, `Run.Enrichment*` ..., status Draft). | POC Data implemented. The other set would be further columns of `Run` in a later revision. **Needs a decision.** |
| W2 | Project fields differ per sheet. POC Data: name, region, businessPurpose, benefit. V-PCREATE (Reviewed): also parent project, study period, owner, description. "04. API Data": cycle, decrement, owner, description, inheritedFrom, state, some with a `project.` prefix. | The four POC fields are required; `description`, `cycle`, `periodFrom`, `periodTo`, `inheritedFrom`, `owner` and `state` are optional (the owner defaults to the caller). |
| W3 | `Project.Period` is Text "normally a year" in sheet 03, a start/end date input in V-PCREATE, and `periodFrom` / `periodTo` in the API sheet. | Both stored: `Project.Period` (`cycle` in the API, as in sheet 04), `Project.PeriodFrom`, `Project.PeriodTo`, all nullable. |
| W4 | `Project.State` has five values in sheet 03 and three in the API sheet. | Five values, CHECK constraint. |
| W5 | Benefit is `array<string>` / multiselect in POC Data and a single enum including "All / Mixed" in sheet 03. | Array (table `ProjectBenefit`, at least one row). "All / Mixed" is seeded but redundant with a multiselect. |
| W6 | `adjustment` is string / select in POC Data; the modelling sheet says multiselect with example `"IBNR","RBNS"`. The modelling sheet also has "Tail treatment" and a separate "IBNR basis" that POC Data lacks. | Implemented as POC Data says (single string). **Needs confirmation.** |
| W7 | The Conditions column is empty for all 36 rows, although the modelling notes describe dependencies. | Two are encoded and marked as inferred in `poc-data.json`: `perTreatyEndDatesMapping` is required when `studyPeriodTreatyOverride` is true (part of `CK_Run_RequiredWhenSubmitted`); `ibnrStudyPeriod` defaults from `studyPeriod` (`defaultFrom` in `GET /profile`, applied by the form). |
| W8 | No option lists for run selects, only one example value each; lists depend on Region / Business Purpose / Benefit / Investigation. | The example values are seeded in `ParameterOption` and offered by `GET /profile`; they are **not enforced**: a run select accepts any text until the real lists exist. A row can be limited to a region, business purpose, benefit or investigation and marked as the default. Region, business purpose and benefit are closed sets (foreign keys). |
| W9 | Booleans use control "select" (Yes/No, On/Off). | Stored and transported as JSON booleans. |
| W10 | API sheet is inconsistent: `{api_url}` vs `{apiUrl}`, `/run/{id}` vs `/runs`, `createRun` without payload, `jobs` typed number but with `.count`. `/datasets`, `/profile`, `/priorbasis`, `/claimmethods`, `/actuals`, `/enrichments` belong to the sheet 02/03 wizard. | The sheet's routes are kept where they name a business thing, with `/runs/{id}` for the single run and camelCase everywhere. `/profile` became `/projects/{id}/run-parameters`; `/datasets` is implemented; the other four are left out. `jobs.count` is real; `analyses.count` is 0 and `portfolioAe` is null until those entities exist. Mapping in [api/README.md](api/README.md). |
| W11 | A run belongs to many projects (`Run.ProjectIds`) and also has a nullable `Run.ProjectId`. | One owning project (`Run.ProjectId`, required), as in the flow described for this work. Re-association on project inheritance is a later migration. |
| W12 | `Run.Locked` is contradictory: locked when submitted, yet "Re-run with fixed configuration" requires `Locked = false` on a failed run. | Locked = any status other than draft. A failed run is resolved with the workbook's `Run.ResolutionAction`: "re-run with fixed config" returns it to draft, "mark resolved" leaves it failed (`POST /runs/{id}/resolution`). |
| W13 | Jobs: FL-CREATE-RUN step 5 says submitting creates a Job; "05. General Questions" asks whether a run can execute without one. Batch runs, bases and analyses are all Draft. | Decision 20: a job is a batch of runs; a single run executes without one. `Job`, `Run.JobId` and the derived job fields (status, counters, progress, duration) are implemented. The bulk builder (template run, vary one field), bases and analyses are not. |
| W14 | Sheet 03 rows 104 and 110 (`Run.SubmittedAt`, `Run.ResolvedAt`) have Description and Type swapped. | Cosmetic. |
| W15 | There is no list of roles and no User entity (sheet 03 models Project, Run, Job, Basis and Analysis). One role is named, in six conditions of the Run details view (sheet 02 rows 235, 236, 253 to 255, 276, all Draft): "the current user is not a Viewer" for re-run, mark resolved, pause or resume, rerun and clone. "Owner" is the user responsible for one Project, Basis or Analysis. "Collaborator" and "owner / preparer" appear only in sheet 05 question 5, which asks what a collaborator may do. | Decision 21: four roles with three permissions. The workbook's Viewer rule is the permission `prepare`, required by every operation that changes a project or a run. `Project.Owner` stays the responsible user and grants nothing. `review` is required by the database for `Project.SignedOffBy` and by no operation yet, because Basis, Analysis and sign-off are not built. **Sheet 05 question 5 is still open.** |

## Audit: code and docs

| # | Finding | Consequence |
|---|---|---|
| C1 | There was no PostgreSQL anywhere: no driver in `requirements.txt` / `pyproject.toml`, no service in `docker-compose.yml`. All state went through the Snowflake connector to the DuckDB-backed emulator. | Greenfield schema: nothing to migrate from. **Done**: `alembic`, `SQLAlchemy` and `psycopg[binary,pool]` are pinned in the requirements; `docker compose --profile gea up` adds `postgres`, `gea-migrate` and `gea-api`. |
| C2 | The docs select Snowflake as the store for contracts: `architecture/README.md` "Published revisions in Snowflake: selected target", `README.md` "Snowflake (or this emulator) is authoritative". | Decision 7 reverses this for GEA application state. **Recorded** in `architecture/README.md` (section "GEA module"); `REQUIREMENTS.md` was not edited. |
| C3 | Same words, different things. "Data Contract" in the repo is a parameter schema plus SQL-function bindings; here it is a frozen run configuration. "Run" in `openapi-v3-draft.yaml` is one asynchronous calculation; here it is a configured study. | Kept apart by the `/gea/v1` prefix and the `gea` schema. Worth renaming one side before both are served together. |
| C4 | The calculation engine works on policies with decimal parameters and scalar SQL functions (`CORE_LIMIT_AMOUNT` ...). GEA steps (segmentation, IBNR chain ladder, A/E) are set-based pipelines. The emulator rejects procedures, table functions and tasks. | "Snowflake reads the contract and runs the steps" cannot be exercised locally. How Snowflake maps a step key to executable logic is designed ([EXECUTION.md](EXECUTION.md)) and drafted as SQL in `db/snowflake`, **not executed**. |
| C5 | No authentication or user identity in `calculation_api/app.py`; CORS allows only `Content-Type`. | Owner, created-by and sign-off need an identity provider. The OpenAPI declares bearer JWT; `gea."User"` maps the subject. **`gea_api`**: CORS allows `Authorization`, `If-Match`, `If-None-Match`, `Idempotency-Key` and exposes `ETag`, `Location`; the token check is a plug-in point (`auth.py`) and the app refuses to start without one. |
| C6 | One process-local `RLock` serialises every writer (`app.py`). | Not carried over: PostgreSQL row locks and optimistic revisions are used instead. Verified with concurrent sessions. |
| C7 | `fixtures/schema.sql` created `PARAMETER_BINDINGS`, `MODEL_OBJECTIVES` and `CALCULATION_RUNS`. The first two were filled by `bootstrap.seed` and read only by `SnowflakeGateway.load`, whose only caller was `smoke_bootstrap.py`; `CALCULATION_RUNS` was used by nothing. | **Removed** on 2 October 2026: the three tables, the seed code, `SnowflakeGateway.load` and `smoke_bootstrap.py`. `CALCULATION_AUDIT` stays (the worker writes it). An emulator database created earlier keeps the unused tables until it is recreated. |
| C8 | `STATE.md` says the draft OpenAPI has "6 paths, 13 schemas"; the file has 6 paths and 16 schemas. The served `openapi.yaml` matches the code (17 paths, 19 operations, 38 schemas; `/openapi.yaml` itself is deliberately excluded). | Doc drift only. |
| C9 | No `.gitignore`; `.idea/` is untracked and `test-results.xml` is committed. | Housekeeping. |
| C10 | The existing test suite (28 tests) needed a DynamoDB endpoint for 3 of them, which were skipped without it. | **Changed**: those tests now run against PostgreSQL (`AUDIT_TEST_DATABASE_URL`); all 28 pass. In `calculation_api` only `audit_export.py`, `bootstrap.seed` and `gateway.py` changed (decision 18, C7). `pyproject.toml` requires Python 3.12; the run was on 3.10, see Verified. |
| C11 | The checked-in previews of architecture pages 9, 11 and 12 (SVG, draw.io, PNG) were older than `architecture/generate.py`: five participants where the README describes six. | **Regenerated.** Pages 1-8 and 10 were already identical to the generator's output. |

## Verified

Run on 2 October 2026, after revision `0007` (user roles), with the real packages installed from PyPI and npm:
Alembic 1.20.0, SQLAlchemy 2.0.54, psycopg 3.3.6 (pool 3.3.3), Litestar 2.24.0, uvicorn 0.52.4, pytest 9.1.1 on
Python 3.10.12 and PostgreSQL 16.2; `openapi-fetch` 0.17.0, `openapi-typescript` 7.13.0, Vue 3.5.43 and
TypeScript 5.9.3 on Node 22.

| Check | Result |
|---|---|
| `spec/poc-data.json` vs workbook sheet "POC Data", rows 5 to 40 (prop, display name, step, type, control, required) | identical, 36 of 36 |
| Every `createRun` prop has a column of `Run` of the matching type; every required prop is in `CK_Run_RequiredWhenSubmitted`; `gea."RunSteps"` returns every prop under its workbook name; no table or column name contains an underscore (`tests/gea/test_migration_sql.py`) | pass |
| `tools/generate.py --check`, `tools/check_openapi.py` (structure and API conventions, the envelope on every response) | up to date; 27 paths, 35 operations, 0 problems |
| Every OpenAPI component schema is valid JSON Schema 2020-12 | 71 of 71 |
| Redocly CLI `lint` of `openapi.yaml`, recommended rules | 0 errors; 3 warnings left on purpose (no licence, localhost server, `/health` has no 4xx) |
| `db/postgres/tests/run.sh`: `alembic upgrade head` on an empty database, again (no-op), `downgrade base`; then every revision alone: applied twice it gives the same schema, and its downgrade leaves exactly the schema of the revision before it (`pg_dump`); `current` = `0007 (head)` | pass, 7 of 7 |
| `alembic upgrade head` on the development database at `0006` that already held 19 users | pass; the 18 people became Admin, the system user Viewer |
| `alembic upgrade head --sql` (offline script), `history` | pass |
| `alembic downgrade` while recorded role changes (below `0007`), jobs (below `0006`), exported audit events (below `0005`) or contracts (below `0003`) exist | each refused, nothing changed, still at `0007` |
| `db/postgres/tests/smoke.sql`, 17 groups: lookups and dropdown values; home region of a user; project; run columns and the required rule; publish and immutability; delivery lease, retry and give-up; execution status, failure and contract v2; draft deletion and locked project; scoped dropdown values; role grants; idempotency receipts; calculation audit copy; jobs in order and `MaxParallel`; resolution of a failure; cancel; execution log and silent executions; roles, the default role, and who may sign off | all pass |
| `python -m pytest tests -W error::DeprecationWarning` with `GEA_TEST_DATABASE_URL` and `AUDIT_TEST_DATABASE_URL` as least-privilege logins: 64 GEA tests and 28 calculation tests | 91 pass, 1 skipped (it adds a `ParameterOption` row, which `gea_app` may not) |
| `python -m pytest tests/gea` as the schema owner | 64 pass, none skipped |
| of these, `tests/gea/test_api.py`: every operation through Litestar's test client and psycopg: the envelope on success and failure, reference data, run parameters, the home region, idempotent creates and the lost-race replay, conditional reads, merge patch, the wizard, review, submit and lock; execution observable from submit to the last step (with the relay's functions called as `gea_relay` would), logs, cancel before and after delivery, both resolutions and contract version 2; a job in order with `maxParallel`, a job without a limit, a job refused as a whole; clone, delete, paging; route table = contract; roles: the four roles and their permissions, the role of a new user, a viewer reads and a preparer works, **every operation that names a permission called by a caller without it (18 calls, each 403)**, the permissions of `services.py` equal `x-permission` of the contract, an Admin gives and takes a role with the change recorded and applied at once, nobody changes their own role | 26 pass |
| of these, `tests/test_audit.py` as a login that is only a member of `calc_audit` | 4 pass |
| `python -m gea_api` under uvicorn, called with curl: envelope, `ETag` and 304, `X-Request-Id`, CORS preflight and exposed headers; 400, 404, 405 and 422 in the envelope; `/users/me`, `/regions`, `/treaties`, `/projects/{id}/run-parameters`, `/jobs`, `/runs/{id}/execution`, `/runs/{id}/logs`, `/roles`, `/users`; a Viewer gets 200 on reads and 403 with the role named on `POST /projects`, `POST /jobs` and `GET /users/{id}` | pass |
| Contracts published by the API validate against `data-contract.schema.json`; their SHA-256 equals `ContentHash` | 100 of 100 |
| Vue client `npm run typecheck` (strict), with `generated.ts` from `generate.py` and from `openapi-typescript`; plus 51 deliberately wrong calls that must not compile | pass with both |
| `useProjects`, `useRunWizard`, `useJobs`, `useJobMonitor`, `useCurrentUser` and `useUsers` on Node through `openapi-fetch` against the running API and PostgreSQL: result object for 404 / 422 / no network, autosave coalescing, debounce, refused save, conflict, review, submit refused after a late change, submit, lock, polling of run and execution and its stop, resume, cancel, both resolutions, a refused job, a job monitored to the 304; `can()` for an Admin and a Viewer, a Viewer refused with 403 as a result, an Admin raising a role that applies at once | 25 checks pass |
| `architecture/generate.py`: 17 pages; the checked-in SVG and draw.io files equal its output. `tools/erd.py`: 19 tables, 36 foreign keys; the checked-in `erd.svg` equals its output on two machines | pass |
| `tools/lucid_export.py`: the committed `db/postgres/lucid-erd-import.csv` equals its output on two machines and on a database whose migrations went down and up again (`tests/run.sh`); compared with `pg_catalog` read independently: 20 tables, 196 columns, 25 primary-key, 38 foreign-key and 15 unique key columns; as the API's login the export is refused (it sees 48 of the 78 key columns) | pass |

The comparison with the workbook ran where the workbook is (it is not in this repository); everything else ran
against the files of this repository. `python -m calculation_api.audit_export --once` against a running emulator
was verified before this change and not run again; nothing it uses changed.

Not verified, and why:

- **The import into Lucidchart.** `lucid-erd-import.csv` is in the format of Lucidchart's own PostgreSQL query and
  was checked against the catalogue, but it has not been imported: that needs a Lucid account.
- **A real identity provider.** The roles were exercised with the development authenticator, which takes the
  caller and the role of a new user from request headers. No token of a real provider has been validated, and
  no role has come from a provider's claim.
- **The sign-off rule through the API.** `Project_BeforeSignOff` (GEA06) is tested in SQL and its mapping to
  403 in a unit test; no operation of the API signs a project off yet, so nothing reaches it over HTTP.
- **The Snowflake side.** `db/snowflake/V001__contract_landing.sql` and `V002__run_pipeline.sql` have not been
  executed anywhere: the local emulator cannot run a stored procedure or a task. The five points to confirm on
  a real account are listed at the end of `V002`; the first decides the design (several graph runs of one task
  graph at the same time). See [EXECUTION.md](EXECUTION.md).
- **The relay.** Not built. Its PostgreSQL functions are tested by calling them directly; nothing has carried a
  contract to Snowflake or a status back.
- **Jobs under load.** Job order and `MaxParallel` are tested with a handful of runs and one claiming session;
  two relays claiming at the same time are serialised by an advisory lock, which was not exercised concurrently.
- **Python 3.12.** `pyproject.toml` requires it; the machine these checks ran on has 3.10. Nothing in
  `gea_api` needs more than 3.10, but the supported interpreter was not the one used.
- **`docker compose --profile gea up`, `--profile audit up`** and the `Dockerfile`: not built (no Docker where this ran). `docker-compose.yml` was only parsed.
- **A browser.** The client ran on Node; CORS was checked with curl, not with a page on another origin.
- **A real form.** There are no Vue components here, only the API client and the composables.

## Open questions

1. Which run field set is in scope: POC Data (implemented) or the sheet 02/03 wizard (W1)?
2. `adjustment`: single value or multiselect? Are "Tail treatment" and "IBNR basis" missing from POC Data (W6)?
3. The real dropdown lists per Region / Business Purpose / Benefit / Investigation, and whether a run select
   must then refuse a value that is not in its list (W8). Today the lists are the workbook's examples.
4. Where do treaties, datasets and the per-treaty end-date mapping come from (Snowflake Module 1 tables)?
   `treaty` is free text and `GET /datasets` serves the two example datasets from `ParameterOption`.
5. How Snowflake executes a contract is proposed in [EXECUTION.md](EXECUTION.md) (step registry, one procedure per
   step, a task graph per contract) and drafted in `db/snowflake`, but never run: it needs a real account, and five
   points confirmed there first (C4). Are the seven steps a chain, or can some run side by side?
6. Status comes back by the relay polling `GEA.CONTROL.RUN_STATUS`, `RUN_STEP_STATUS` and `RUN_LOG`. The relay is
   not built. How long may an execution be silent before it is failed (30 minutes today)?
7. Roles (decision 21) are built; these points about them are not decided:
   - Who is the identity provider, and does it own the role? Today the provider's role is taken when a user is
     first seen and an Admin changes it afterwards. The first Admin of an installation comes from the provider's
     claim or from one `UPDATE gea."User"` by the database administrator.
   - Sheet 05 question 5: may a collaborator do everything an owner may? Today `Project.Owner` grants nothing:
     every user who may prepare can change every project.
   - Maker and checker: should the user who prepared a Basis or an Analysis be barred from reviewing it? Nothing
     enforces it yet; the place is the review and sign-off operations, which are not built.
   - Should an Admin be able to sign off? Today the Admin role carries all three permissions.
   - Should the home region of a user limit which projects the user sees? Today it only preselects the region.
8. Can a run be shared between projects (W11)? Should a failed run stop the rest of a sequential job (today it
   does not)? The job bulk builder of sheet 03 (template run, vary one field) is not built.
9. Can `region` and `businessPurpose` of a project change after runs exist? Currently fixed at creation.

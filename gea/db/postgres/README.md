# PostgreSQL schema and migration plan

Schema `gea`, PostgreSQL 14 or newer (verified on 16). No extensions are required. Migrations are
[Alembic](#migrations) revisions that run hand-written SQL.
Decisions and audit findings are in [../../README.md](../../README.md); how the API uses these
tables is in [../../api/README.md](../../api/README.md).

## Naming

Tables and columns carry the names of the workbook's model (sheet "03. Model Data": `Project.BusinessPurpose`,
`Run.Treaty`) and of its field list (sheet "POC Data": `createRun.ibnrMethodology` is `"Run"."IbnrMethodology"`).

| Object | Rule | Example |
|---|---|---|
| Table | PascalCase, singular | `gea."Run"`, `gea."ProjectBenefit"` |
| Column | PascalCase; a foreign key ends in `Id` | `"StudyPeriodStart"`, `"RegionId"` |
| Primary key, foreign key, unique, check | `PK_<Table>`, `FK_<Table>_<Target>`, `UQ_<Table>_<Columns>`, `CK_<Table>_<Rule>` | `"FK_User_Region"`, `"CK_Run_RequiredWhenSubmitted"` |
| Index | `IX_<Table>_<Columns>` | `"IX_Run_ProjectId"` |
| Function, view, trigger | PascalCase; a trigger is `<Table>_<When>` | `gea."RunSteps"`, `gea."ProjectSummary"`, `"Run_BeforeUpdate"` |
| Lookup id | lower-case kebab text, the value the API uses | `'north-america'`, `'rnd'` |

PostgreSQL folds an unquoted name to lower case, so every identifier is written in double quotes, in the
migrations and in the API's SQL: `SELECT "Name" FROM gea."Run"`. Without the quotes the name is not found.
The schema names (`gea`, `calc`) and the roles are lower case and need none.

## Model

```mermaid
erDiagram
    Region ||--o{ User : "home region"
    Role ||--o{ User : "may do"
    Region ||--o{ Project : ""
    BusinessPurpose ||--o{ Project : ""
    User ||--o{ Project : owns
    Project ||--|{ ProjectBenefit : has
    Benefit ||--o{ ProjectBenefit : ""
    Project ||--o{ Run : contains
    Project |o--o{ Job : "all runs of"
    Job ||--o{ Run : "submitted together"
    Run ||--o{ RunLog : "logs"
    Run ||--o{ RunStudyPeriodExclusion : excludes
    Run ||--o{ DataContract : "published as"
    DataContract ||--|{ ContractDelivery : "delivered by"
    DataContract ||--o| RunExecution : "executed as"
    RunExecution ||--|{ RunExecutionStep : reports
    Region |o--o{ ParameterOption : "limits"
    BusinessPurpose |o--o{ ParameterOption : "limits"
    Benefit |o--o{ ParameterOption : "limits"
```

| Table | Purpose | Mutability |
|---|---|---|
| `Region`, `BusinessPurpose`, `Benefit` | The closed lists of `createProject` (lookup tables, not ENUM types, so a value can be added without DDL) | reference data |
| `Role` | What a user may do: Viewer, Preparer, Reviewer, Admin, each with `CanPrepare`, `CanReview`, `CanAdminister`. A lookup table like `Region`, so a role or what it allows changes without DDL | reference data |
| `User` | Callers, mapped from the identity provider subject. `RegionId` (required) is the user's home region, `RoleId` (required, `viewer` for a new user) the role | insert / update |
| `Project` | `Project.*` of sheet 03 and the members of `GET /projects` in sheet 04: name, business purpose, region, period, owner, state, description, parent project, sign-off | optimistic `Revision`; locked when `signed-off` |
| `ProjectBenefit` | `createProject.benefit`: one row per benefit, at least one (checked at commit) | with the project |
| `Run` | The run and its whole configuration: **one typed column per `createRun` prop**, then lifecycle (`Status`, `Locked`, `CloneSourceId`, `CurrentContractVersion`, `SubmittedAt/By`, `FailureMessage`, the decision on a failure `Resolution*`, `CancelRequestedAt/By`) and its place in a job (`JobId`, `JobOrdinal`) | configuration only while `draft` |
| `Job` | Several runs submitted together: name, kind, note, `MaxParallel` (1 = one after another, empty = no limit), who and when. Its status is derived from its runs (view `JobSummary`) | insert-only |
| `RunStudyPeriodExclusion` | `createRun.studyPeriodExclusions`: the list of excluded periods, in order | only while the run is `draft` |
| `ParameterOption` | The values a dropdown offers per prop, optionally limited to a region, business purpose, benefit or investigation, with a default | reference data |
| `DataContract` | The frozen document: canonical text, generated `jsonb`, SHA-256, `(RunId, Version)` unique | **immutable** |
| `ContractDelivery` | Outbox: one row per contract and target (`snowflake`) with lease, attempts, backoff; `cancelled` when the run was cancelled before it was sent | via functions only |
| `RunExecution`, `RunExecutionStep` | Execution status reported by Snowflake for one contract | via function only |
| `RunLog` | What the execution reported, line by line (step, level, message); a line is stored once per `ExternalId` | insert-only (trigger), via function only |
| `CommandReceipt` | Stored answer per caller and `Idempotency-Key`, so a repeated request gets the first answer | insert-only by convention |
| `AuditEvent` | Append-only business events | insert-only (trigger) |
| `AlembicVersion` | The Alembic revision the database is at (one row; its column `version_num` is Alembic's) | Alembic only |

Views `ProjectSummary` (benefits, owner, `TotalRuns`, `CompletedRuns`, `ActiveRuns`, `FailedRuns`,
`UnresolvedFailedRuns`, `DraftRuns`, `JobCount`), `RunSummary` (project, current contract, delivery status,
job, resolution, cancel request) and `JobSummary` (run counters, `Status`, `ProgressPercentage`, `FinishedAt`)
back the list endpoints. `RunCancelRequest` is what the relay reads to pass a cancel on to Snowflake.

Every workbook field with its column is listed in [../../spec/fields.md](../../spec/fields.md) (generated).
In short:

| Workbook | Stored as |
|---|---|
| `createProject.name`, `region`, `businessPurpose` | `Project.Name`, `Project.RegionId`, `Project.BusinessPurposeId` |
| `createProject.benefit` (`array<string>`) | rows of `ProjectBenefit` |
| `createRun.name`, `treaty` | `Run.Name`, `Run.Treaty` |
| `createRun.<prop>` of type `string` | `Run.<Prop>` `text` |
| `createRun.<prop>` of type `boolean` | `Run.<Prop>` `boolean` |
| `createRun.<prop>` of type `array<string>` | `Run.<Prop>` `text[]` |
| `createRun.studyPeriod`, `ibnrStudyPeriod` (`{start, end}`) | `Run.StudyPeriodStart` / `StudyPeriodEnd`, `Run.IbnrStudyPeriodStart` / `IbnrStudyPeriodEnd` `date` |
| `createRun.studyPeriodExclusions` (`array<{start, end}>`) | rows of `RunStudyPeriodExclusion` (`Ordinal`, `StartDate`, `EndDate`) |
| `Project.Period`, `Owner`, `State`, `Description`, `ParentProjectId`, `Locked`, `SignedOffAt`, `SignedOffBy` (sheet 03) | columns of `Project` with the same names (`OwnerId` for the owner) |
| `periodFrom`, `periodTo`, `inheritedFrom` (sheet 04) | `Project.PeriodFrom`, `Project.PeriodTo`, `Project.ParentProjectId` |
| `Run.Status`, `Locked`, `CloneSourceId`, `SubmittedAt`, `SubmittedBy`, `FailureMessage`, `ProjectId`, `Created` (sheet 03) | columns of `Run` with the same names (`CreatedAt`) |
| `Project.RunCount`, `TotalRuns`, `CompletedRuns`, `FailedRuns`, `ActiveRuns` (sheet 03, derived) | view `ProjectSummary` |

The wizard steps are not tables. `gea."RunSteps"(runId)` returns the configuration grouped into the steps of
the workbook, in order, with the workbook's prop names; that is what goes into the data contract.

### Schema `calc`: the calculation module's audit copy

One table that is not part of GEA lives in the same database, in its own schema, and in the same migration
history (revision `0005`):

| Table | Purpose | Mutability |
|---|---|---|
| `calc."AuditEvent"` | Copy of the calculation module's audit events (`INSURANCE.CALC.AUDIT_EVENTS` in Snowflake), one row per event id: canonical text, generated `jsonb`, SHA-256 | insert-only (trigger); `calc_audit` may only `INSERT` and `SELECT` |

`calculation_api.audit_export` writes it from the SQL outbox; see the root [README](../../../README.md#audit-storage).
Snowflake stays authoritative for those events, so nothing in GEA reads this table.

![Entity-relationship diagram of schema gea](erd.svg)

`erd.svg` is drawn from the live catalogue, so it cannot drift from the migrations. After a schema change:

```bash
python gea/tools/erd.py --database-url "$GEA_DATABASE_URL"      # needs psql and Graphviz dot
```

### The schema in Lucidchart

[`lucid-erd-import.csv`](lucid-erd-import.csv) is the schema in the format Lucidchart imports as an
entity-relationship diagram: 20 tables (schema `gea` and `calc."AuditEvent"`), every column with its type, and
the primary, foreign and unique keys.

1. In Lucidchart open **More shapes** at the bottom of the shape panel, tick **Entity Relationship**, and choose
   **Use selected shapes**.
2. In the Entity Relationship library click **Import**, choose **PostgreSQL**, and go to the next step without
   running the query it shows: this file is the result of that query.
3. **Choose File**, pick `lucid-erd-import.csv`, **Import**.
4. The tables appear in the shape panel. Drag the ones you want onto the canvas; Lucidchart draws a line for
   every foreign key between two tables that are both on the canvas.

The file is written by a tool, from the live catalogue, and `tests/run.sh` fails when it is not the schema the
migrations give. After a schema change:

```bash
python gea/tools/lucid_export.py --database-url "$GEA_DATABASE_URL"      # as the owner of the schema; needs psql
```

What the file is, and what it is not:

- It is the result of the query Lucidchart gives for PostgreSQL, with five differences that keep the shape of
  a row: only the schemas `gea` and `calc`; a fixed database name (`gea`); `text[]` where the catalogue view
  says `ARRAY`; columns numbered 1, 2, 3 ... per table (the catalogue leaves a gap where a column was once
  dropped); and a fixed order of rows. With these the file is the same whichever database it is read from
  and whichever path its migrations took.
- One row per column and per key it belongs to, so `Run.Id` has three rows: primary key, the unique key
  `(Id, ProjectId)`, and the foreign key to its current contract. That is what Lucidchart's own query returns.
- Lucidchart's format has no place for `NOT NULL`, defaults, CHECK constraints, triggers, views or generated
  columns. The rules of this schema that live there are in the table below, not in the diagram.
- Run it as the owner of the schema. `information_schema` shows the keys of a table only to its owner or to a
  role that may write it; as the API's login the query returns 48 of the 78 key columns, and Lucidchart would
  draw only some of the relationships. The tool counts the keys in `pg_catalog` and refuses an incomplete result.
- **Not tried in Lucidchart.** The file was checked against `pg_catalog` (tables, columns, types, keys); the
  import itself needs a Lucid account.

### Rules the database enforces

Each rule is exercised by `tests/smoke.sql`, except TRUNCATE, which cannot be attempted inside the test's own transaction and was checked separately.

| Rule | Mechanism |
|---|---|
| A user has a home region; a project has a region, a business purpose and an owner that exist | `NOT NULL` + foreign keys (`FK_User_Region`, `FK_Project_Region` ...) |
| A user has exactly one role, and it exists; a new user is a Viewer | `"RoleId" NOT NULL DEFAULT 'viewer'`, `FK_User_Role` |
| A project is signed off by a user whose role may review, whoever writes the row | `Project_BeforeSignOff` (SQLSTATE `GEA06`) |
| A project has at least one benefit | deferred constraint triggers `Project_RequiresBenefit`, `ProjectBenefit_RequiresOne` |
| A signed-off project cannot be changed; no run can be created in it | `Project_BeforeUpdate`, `Run_BeforeInsert` (SQLSTATE `GEA03`) |
| A draft run may have any configuration column empty; a date range has both ends or neither, in order | columns are nullable; `CK_Run_StudyPeriod`, `CK_Run_IbnrStudyPeriod` |
| A run can leave `draft` only with every prop the workbook marks as required, and with the end-date mapping when the treaty override is on | `CK_Run_RequiredWhenSubmitted` |
| The configuration of a run (every column that is not lifecycle) and its excluded periods can change only while the run is `draft` | `Run_BeforeUpdate`, `RunStudyPeriodExclusion_BeforeWrite` (`GEA03`) |
| A run moves only draft -> queued -> running -> complete or failed, and failed -> draft | `Run_BeforeUpdate` (`GEA04`) |
| Only a failed run is resolved; a resolution has an action, a person and a time; `mark-resolved` keeps the run failed, `rerun-with-fixed-config` returns it to draft | `CK_Run_Resolution`, `CK_Run_ResolutionState`, `Run_BeforeUpdate` (`GEA04`) |
| A cancel can be requested only while the run is queued or running. Before the contract is sent the delivery is withdrawn and the run fails at once; afterwards the request is recorded for Snowflake | `gea."RequestRunCancel"`, `Run_BeforeUpdate` (`GEA04`) |
| A run belongs to a job with its position, or to none; a position is used once per job | `CK_Run_Job`, `UQ_Run_JobId_JobOrdinal` |
| A submitted run cannot be deleted | `Run_BeforeDelete` (`GEA03`) |
| A contract can be inserted only for a draft run, with the next version number, and only if its document equals what `gea."BuildContractDocument"` builds from the saved columns | `DataContract_BeforeInsert` (`GEA05`); `FOR UPDATE` on the run row |
| The hash is the SHA-256 of the stored canonical text; id, run, project and version in the document equal the columns | `CK_DataContract_HashMatchesDocument`, `CK_DataContract_DocumentMatchesColumns` |
| Publishing queues the Snowflake delivery and locks the run in the same transaction | `DataContract_AfterInsert` |
| A contract is never updated, deleted or truncated | `ForbidMutation` triggers; no UPDATE/DELETE grant |
| A delivery is owned by one relay at a time; a lost lease cannot complete or fail it | `ClaimContractDeliveries`, `CompleteContractDelivery`, `FailContractDelivery` |
| The runs of a job start in job order, at most `MaxParallel` at a time; a run on its own starts at once | `ClaimContractDeliveries` (one claim at a time, advisory lock) |
| A log line is stored once, and never changed | `gea."RecordExecutionLog"` (`UQ_RunLog_ExternalId`), `ForbidMutation` triggers |
| An execution that stops reporting does not stay `running` | `gea."FailStaleExecutions"(interval)`, called by the relay |

What a role may do through the API (create, submit, administer) is checked by the API, which answers 403; see
[../../api/README.md](../../api/README.md#roles-and-permissions). The database holds the roles and the one
rule about them that belongs to the data. The API login reads `Role` and sets `User.RoleId`; it cannot change
what a role allows.

`SECURITY DEFINER` is used only where a database role must cause a write it may not perform directly
(delivery rows, execution status); those functions pin `search_path` and are not executable by `PUBLIC`.

Not enforced by the database, on purpose:

- **The value of a run select.** `Run.ExposureMethod`, `Run.IbnrMethodology` ... are `text` and accept any value.
  `ParameterOption` holds the workbook's example values and is what the dropdowns offer; the real lists do not
  exist yet (open question 3). When they do, a trigger or foreign key can make them binding.
- **`Run.Treaty`** is free text until treaties come from Snowflake Module 1 (open question 4).

## Migrations

Alembic, SQL-first. A revision is a small Python file in `migrations/versions/` plus two SQL files in
`migrations/sql/`; the Python file only says which revision it follows and runs the SQL.

```text
gea/db/postgres/
  alembic.ini                     script location, file naming; no database URL in it
  migrations/env.py               URL from GEA_DATABASE_URL, advisory lock, lock_timeout, schema and name of the version table
  migrations/gea_sql.py           run_sql(__file__, "up" | "down"): splits a .sql file into statements
  migrations/script.py.mako       template of a new revision
  migrations/versions/0001_baseline.py ... 0007_user_roles.py
  migrations/sql/<revision>.up.sql and <revision>.down.sql
```

Why SQL files and not `op.create_table`: most rules of this schema live in triggers, PL/pgSQL functions,
CHECK constraints and views, which Alembic's autogenerate cannot see. There is no ORM model to compare with
(`target_metadata = None`), so `alembic revision --autogenerate` is not used.

| Revision | Content |
|---|---|
| `0001_baseline` | Shared trigger functions; `Region`, `BusinessPurpose`, `Benefit` with their workbook values; `User` with its home region; `CommandReceipt`; `AuditEvent` |
| `0002_project_and_run` | `Project`, `ProjectBenefit`, `Run` (one column per `createRun` prop), `RunStudyPeriodExclusion`, lifecycle triggers, `gea."RunSteps"`, `gea."RunConfiguration"`, `ParameterOption` with the workbook's example values |
| `0003_data_contract` | `DataContract`, `ContractDelivery`, `gea."BuildContractDocument"`, publication and immutability triggers, `RunExecution`, `RunExecutionStep`, the relay functions, views `ProjectSummary` and `RunSummary` |
| `0004_privileges` | Group roles `gea_app` and `gea_relay` (created when missing) and their grants |
| `0005_calculation_audit` | Schema `calc` with `calc."AuditEvent"`, the append-only copy of the calculation module's audit events, and the group role `calc_audit` (created when missing) |
| `0006_jobs_and_execution_control` | `Job`; on `Run` the job position, the resolution of a failure and the cancel request; `RunLog`; `gea."RequestRunCancel"`, `gea."RecordExecutionLog"`, `gea."FailStaleExecutions"`; delivery in job order; views `JobSummary`, `RunCancelRequest`; grants |
| `0007_user_roles` | `Role` with its four rows and three permissions; `User.RoleId` (existing users become Admin, the seeded system user Viewer); `Project_BeforeSignOff`; `AuditEvent` accepts events of users |

Revisions `0001` to `0005` replace the ten of the first design. No database had been deployed, so the history was
rewritten instead of adding rename revisions. A local database that was created from the old revisions has to
be dropped and created again.

```bash
pip install -r requirements.txt
export GEA_DATABASE_URL=postgresql://gea_owner:secret@localhost:5432/gea
alias gea-alembic='alembic -c gea/db/postgres/alembic.ini'

gea-alembic upgrade head            # everything; one transaction, all or nothing
gea-alembic upgrade head --sql      # print the SQL instead of running it (for a DBA review)
gea-alembic current                 # where the database is
gea-alembic history                 # the revisions
gea-alembic downgrade -1            # undo the last revision (development)
gea-alembic upgrade 0002            # stop after 0002

# scratch database "gea_test": upgrade, upgrade again, downgrade to base, upgrade, smoke test
GEA_ADMIN_URL=postgresql://postgres@localhost:5432/postgres bash gea/db/postgres/tests/run.sh
```

What `env.py` does beyond the Alembic default:

- **One migration at a time.** It takes a session advisory lock before reading the current revision, so two
  deployments that start together apply each revision once.
- **`lock_timeout` of 10 s** (`-x lock_timeout=30s` to change). DDL that cannot get its lock fails instead of
  queueing behind a long transaction and blocking every other session on that table.
- **The version table is `gea."AlembicVersion"`.** Everything the application owns is in one schema, and
  PostgreSQL 15+ no longer lets ordinary roles create tables in `public`. `env.py` creates the schema when
  it is missing; that is why `0001` does not.
- **One transaction for the whole upgrade** (PostgreSQL DDL is transactional).
- **psycopg 3**: a URL that starts with `postgresql://` is run with the `psycopg` driver.

### Adding a revision

```bash
gea-alembic revision --rev-id 0008 -m "add treaty tables"
# creates migrations/versions/0008_add_treaty_tables.py; add next to it:
#   migrations/sql/0008_add_treaty_tables.up.sql
#   migrations/sql/0008_add_treaty_tables.down.sql
```

Rules for the SQL files (checked by `tests/gea/test_migration_sql.py`):

- Plain SQL only: no psql meta-commands (`\i`, `\set`) and no `BEGIN` / `COMMIT`. Alembic owns the transaction.
- Qualify every object with `gea.` and quote every identifier; no `SET search_path`. All revisions share one session.
- Table and column names are PascalCase, constraints carry their `PK_` / `FK_` / `UQ_` / `CK_` prefix.
- Write the downgrade. It drops what the upgrade created, in reverse order. A downgrade that would destroy
  records that must be kept refuses instead (see `0003_data_contract.down.sql`, `0006_jobs_and_execution_control.down.sql`,
  `0007_user_roles.down.sql`).
- A revision that changes a function, trigger or view of an earlier one restores the earlier text in its downgrade
  (`tests/run.sh` compares the schema after `downgrade -1` with the schema of the revision before).
- A revision that adds a table, sequence or function also grants on it to `gea_app` / `gea_relay`. There is
  no repeatable grants script.
- `CREATE INDEX CONCURRENTLY` cannot run in a transaction: give it its own revision and wrap the statement
  in `with op.get_context().autocommit_block():` in the Python file.

### A new or changed run field

1. Edit `gea/spec/poc-data.json` (prop, display name, step, type, control, required, column).
2. New revision: `ALTER TABLE gea."Run" ADD COLUMN "<Prop>" <type>`; add it to `gea."RunSteps"` (the step it
   belongs to) and, when the workbook marks it required, to `CK_Run_RequiredWhenSubmitted`
   (`ADD CONSTRAINT ... NOT VALID`, then `VALIDATE`, when runs already exist).
3. `python gea/tools/generate.py` rewrites the API schemas, the contract schema, the TypeScript types and `fields.md`.
4. `tests/gea/test_migration_sql.py` compares the field list with the columns and fails until they agree.
5. `python gea/tools/erd.py` and `python gea/tools/lucid_export.py` redraw the diagram and the Lucidchart import
   file; `tests/run.sh` fails while the latter is stale.

## Migration plan

There is no PostgreSQL database in any environment yet, so this is a first installation, not a data
migration. The fixture data in the Snowflake emulator (policies, functions, core models) belongs to the
calculation proof of concept and is not moved.

### Phase 0: prerequisites

1. Provision PostgreSQL 16 (RDS or Aurora in the same AWS region as the API) with a migration login that
   owns the schema (`gea_owner`) and two login users for the API and the relay.
2. These are **database** roles, the logins of the services; the roles of the application's users are rows of
   `gea."Role"`. The group roles `gea_app` and `gea_relay` are created by revision `0004` when the migration login has
   `CREATEROLE`; otherwise create them first (`CREATE ROLE gea_app NOLOGIN`). Make the API login a member
   of `gea_app` and the relay login a member of `gea_relay`. Revision `0005` does the same for `calc_audit`,
   the role of the calculation module's audit exporter.
3. Locally: `docker compose --profile gea up` starts `postgres`, runs `gea-migrate` (`alembic upgrade head`)
   and then `gea-api`, which starts only after the migration has succeeded.
4. Decide the open questions that change columns (field set, `adjustment`) before data exists; afterwards
   they are a new revision with a data migration.

### Phase 1: foundation, 0001 and 0002

Projects, runs and wizard saving work end to end with no Snowflake dependency. The API endpoints for them are
implemented in `gea_api` and covered by `tests/gea/test_api.py`.

### Phase 2: contract, 0003

Submitting produces immutable contracts and pending deliveries. Deliveries stay `pending` until the relay
exists, which is safe. `POST /runs/{runId}/submit` is implemented, including the canonical JSON hash.

### Phase 3: jobs and execution control, 0006

Jobs, delivery in job order, cancel, resolution of a failure, the run log and the handling of an execution
that went silent. Implemented in the database and the API (`POST /jobs`, `/runs/{runId}/cancel`,
`/runs/{runId}/resolution`, `/runs/{runId}/logs`); smoke groups 13 to 16.

### Phase 3b: user roles, 0007

`Role`, `User.RoleId` and the permission checks of the API (`GET /roles`, `GET /users`, `PATCH /users/{userId}`,
403 on every operation that changes something); smoke group 17. Before the first sign-in after this revision,
decide who the first Admin is: the identity provider's claim, or one `UPDATE gea."User" SET "RoleId" = 'admin'`.

### Phase 4: Snowflake hand-over, `db/snowflake/V001` and `V002`

Relay process (claim, MERGE, read back the hash, `EXECUTE TASK`, complete or fail; copy status and log back;
pass cancel requests on; call `FailStaleExecutions`) and the pipeline in Snowflake. **Not built**; the
Snowflake SQL is a draft that has never been executed. See [../../EXECUTION.md](../../EXECUTION.md).
Exit criteria: smoke groups 6, 7 and 13 to 16 (they pass), plus an integration test against a real Snowflake
account, since the local emulator cannot run the landing DDL, a stored procedure or a task.

### Phase 5: later revisions, when the workbook sections leave Draft

| Adds | Blocked on |
|---|---|
| `ProjectRun` (a run re-associated with another project on inheritance) | W11 |
| Batch *creation* (many runs generated from one configuration) and the job kinds of sheet 02 beyond "several runs submitted together" | W13 |
| `Treaty`, `Dataset` filled from Snowflake Module 1; `Run.Treaty` and `Run.DataScope` then reference them | open question 4 |
| The real dropdown lists in `ParameterOption`, and the rule that makes them binding | open question 3 |
| `TreatyGroup`, `Basis`, `Analysis` and sign-off; `analyses.count`, `portfolioAe` | sheets 02/03 leaving Draft |
| The run fields of the sheet 02/03 wizard as further columns of `Run` | W1 |

### Rules for every later revision

- Never edit a revision that has been applied anywhere. Add a new one.
- Expand, then contract: add nullable columns or new tables first, deploy the API that writes both shapes,
  backfill, and only then add `NOT NULL` or drop the old shape in a later revision. Old and new API versions
  overlap during a rolling ECS deployment, so each revision must be compatible with the previous API version.
- Large tables: `CREATE INDEX CONCURRENTLY` (own revision, see above) and `ADD CONSTRAINT ... NOT VALID`
  followed by `VALIDATE CONSTRAINT`.
- `DataContract` and `AuditEvent` are never rewritten. A change of document format is a new `specVersion`
  handled by readers; a new run field appears in contracts published after it, old contracts stay as they are.
- Production rolls forward: a failed upgrade rolls back by itself (one transaction); a bad release is fixed
  by the next revision. Downgrades are for development and for the test that proves every revision reverses.
  Take a snapshot before a release that drops anything.
- CI on every pull request: `tools/generate.py --check`, `tools/check_openapi.py`, `tests/run.sh` against a
  `postgres:16` service, then `pytest tests/gea` with `GEA_TEST_DATABASE_URL`.
- Run `alembic upgrade head` as a deployment step before the new API tasks start, never from application
  start-up.

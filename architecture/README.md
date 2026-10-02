# Calculation modeling architecture — revision 3

Design date: **17 September 2026**. This directory is the single architecture
reference for the insurance calculation workflow. It describes the
**revision 3 target** — a Data Contract module and the logical
Bronze → Core Calculation → Silver → custom calculation → Gold layers —
together with the **implemented runtime** those layers execute on. The runtime
currently exposes the version 2 lifecycle; the gap between the two is tracked
in [../STATE.md](../STATE.md). Requirements are in
[../REQUIREMENTS.md](../REQUIREMENTS.md), the HTTP contract in
[../api/README.md](../api/README.md), and the request walk-through with the
worked policy example in [../FLOW.md](../FLOW.md).

Start with [Core phase, then custom runs](09-parameter-layer-sequence.svg).
All six participants are at the top, with business and technical names,
including **DataContract** and **Custom Configuration**. Core first retrieves
parameter definitions and source mappings from DataContract to model Bronze
data into Silver. For every new custom run, Python loads both DataContract and
the selected Custom Configuration as independent inputs and combines them
with the source model after validation. Regional is the worked custom example.
The interaction references expand into [background execution](11-assessment-client-updates.svg)
and [retry handling](12-calculation-retry-sequence.svg). The
[deployment map](10-assessment-worker.svg) explains the supporting services.

## Contents

| File | Purpose |
|---|---|
| `README.md` | This document: target design, implemented runtime, required changes |
| `calculation-architecture.drawio` | Seventeen editable diagram pages (draw.io; uncompressed XML) |
| `01-…` to `17-….svg` | Vector previews of the same seventeen pages |
| `03-….png`, `09-….png` to `12-….png` | Raster previews of regional scenarios, the model example, responsibilities and actor sequences |
| `data-contract.example.yaml` | Proposed Data Contract for the four seeded parameters |
| `death-penatly-contract.example.yaml` | Proposed one-property Data Contract used by the business and retry sequences |
| `custom-configuration.example.yaml` | Separate Regional Calculation configuration: process, formula binding, factor and input override |
| `insurance-custom-configuration.example.yaml` | Separate Custom Configuration for the four-parameter integration example |
| `generate.py` | Regenerates the draw.io file and every SVG from one scene model |

## Diagrams

| Page | Scope | Question answered | Preview |
|---|---|---|---|
| 1 — Layer overview | target | How do Bronze, Core Calculation, Silver, custom calculations and Gold connect, and what controls them? | [01-layer-overview.svg](01-layer-overview.svg) |
| 2 — Data Contract | target | How does a published contract become a frozen execution plan that the database executes? | [02-data-contract.svg](02-data-contract.svg) |
| 3 — Silver to Gold | target | How do scenario edits, reruns and retries produce versioned Gold models without touching Silver? | [03-silver-to-gold.svg](03-silver-to-gold.svg) |
| 4 — Components | runtime | What runs where, and how do Vue, Litestar, the Snowflake emulation and the PostgreSQL audit copy connect? | [04-components.svg](04-components.svg) |
| 5 — Functions and bindings | runtime | How is SQL edited, published, selected per parameter, retrieved and executed? | [05-functions-and-bindings.svg](05-functions-and-bindings.svg) |
| 6 — Calculation sequence | runtime | What is inside `calculate()`, and which methods execute SQL and commit results? | [06-calculation-sequence.svg](06-calculation-sequence.svg) |
| 7 — Audit sequence | runtime | When is an audit durable, and how does the export to PostgreSQL recover from failures? | [07-audit-sequence.svg](07-audit-sequence.svg) |
| 8 — Contract storage and AWS orchestration | target | Where is a contract published, how is a run queued and claimed, and how are results delivered? | [08-contract-storage-aws-orchestration.svg](08-contract-storage-aws-orchestration.svg) |
| 9 — Core phase, then custom runs | main target sequence | How does Core model Bronze using DataContract, and how does a later custom run combine independently loaded DataContract, Custom Configuration and source model? | [09-parameter-layer-sequence.svg](09-parameter-layer-sequence.svg) |
| 10 — The business calculation journey | target, business and technology view | Which part uses Amazon Simple Queue Service, what is the Python worker, and where is DeathPenatly calculated? | [10-assessment-worker.svg](10-assessment-worker.svg) |
| 11 — Background execution detail with DataContract and Custom Configuration | supporting target sequence | How are both inputs loaded, validated and frozen before dispatch, execution and client updates? Regional is the detailed example. | [11-assessment-client-updates.svg](11-assessment-client-updates.svg) |
| 12 — Automatic and user-requested retry | target, actor sequence | How does retry preserve the original model, DataContract, Custom Configuration and execution plan? | [12-calculation-retry-sequence.svg](12-calculation-retry-sequence.svg) |
| 13 — GEA components | GEA, implemented | What runs where: the Vue client, the Litestar GEA API, PostgreSQL and the Alembic migrations? | [13-gea-components.svg](13-gea-components.svg) |
| 14 — GEA data model | GEA, implemented | Which tables hold the lookups, users with their role, projects, runs with their workbook fields, jobs, contracts and execution status, and how are they related? | [14-gea-data-model.svg](14-gea-data-model.svg) |
| 15 — GEA wizard save | GEA, implemented, actor sequence | How is the run autosaved while the wizard is filled in, validated and protected against a concurrent edit? | [15-gea-wizard-save.svg](15-gea-wizard-save.svg) |
| 16 — GEA submit and delivery | GEA, API implemented, relay target | How does a reviewed run become an immutable contract, and how does it reach Snowflake? | [16-gea-submit-delivery.svg](16-gea-submit-delivery.svg) |
| 17 — GEA run execution | GEA, PostgreSQL and API implemented; Snowflake pipeline is draft SQL; relay target | Who computes a run, who decides when it starts, how is its state observed, and how are runs ordered or run in parallel? | [17-gea-run-execution.svg](17-gea-run-execution.svg) |

Pages 1–3 show the target data layers; page 8 shows the proposed AWS deployment.
Page 9 is the main sequence: Core first, then a custom process with input overrides.
Its six participant headers pair business roles with concrete technical names.
Page 10 is a responsibility map. Pages 11–12 are supporting execution sequences,
with participants at the top, lifelines, requests/returns, parallel client
checks, success/failure alternatives and retry loops.
Pages 13–17 show the GEA module and are described under
[GEA module](#gea-module-pages-1317). Pages 4–7 show the implemented version 2 runtime
the layers execute on and are checked against the source files listed under
[Source traceability](#source-traceability). All draw.io text uses explicit
dark font colours and white label backgrounds (`html=0`,
`labelBackgroundColor=#FFFFFF`); SVG files are vector previews that stay sharp
when zoomed. An import into the Lucidchart application has not been exercised.

## GEA module (pages 13–17)

Pages 1–12 describe the calculation module. Pages 13–17 describe the GEA module
(projects, the run wizard and run data contracts), which lives in
[`../gea`](../gea/README.md) and [`../gea_api`](../gea_api) and runs as its own
process. The two share this repository and the Snowflake account, not a runtime
or a store.

**Storage decision for GEA.** The section [Contract storage and publication](#contract-storage-and-publication-target)
selects Snowflake as the store of the calculation Data Contract. For GEA the
system of record is **PostgreSQL**: a run's contract and its pending Snowflake
delivery are committed in one PostgreSQL transaction, and a relay delivers the
identical document (same id, same SHA-256) to Snowflake afterwards. Writing to
both stores from the request could leave a contract in one that the other never
committed. The reasoning is decision 7 in [`../gea/README.md`](../gea/README.md).

The same words mean different things in the two modules:

| Word | Calculation module (pages 1–12) | GEA module (pages 13–17) |
|---|---|---|
| Data Contract | Parameter schema plus SQL-function bindings, published in Snowflake | The frozen configuration of one submitted run, stored in PostgreSQL and copied to Snowflake |
| Run | One asynchronous calculation | A configured study inside a project, built in the wizard |
| Revision | Published version of a contract, function or scenario | Counter on a row; optimistic concurrency uses the ETag of the representation |

The full column list of the GEA schema is drawn from the live catalogue by
`python gea/tools/erd.py` into [`../gea/db/postgres/erd.svg`](../gea/db/postgres/erd.svg);
page 14 shows the keys, the rules and the run fields grouped by wizard step. Tables and columns carry the names of the workbook, in PascalCase.

**Execution of a GEA run.** Snowflake computes: one stored procedure per workbook step, ordered by a task graph, one graph run per
contract. PostgreSQL decides when a run may start (a run on its own at once; the runs of a job in order, `MaxParallel` at a time) and
keeps the status the user sees. Python only carries messages between the two. Page 17 shows the three parts;
[`../gea/EXECUTION.md`](../gea/EXECUTION.md) gives the reasoning and what is built. The Snowflake SQL is a draft that has not been executed.

**Roles of GEA users.** A user has one role (`User.RoleId` → `Role`: Viewer, Preparer, Reviewer, Admin) and a role is three permissions,
`CanPrepare`, `CanReview` and `CanAdminister`. The API asks for a permission, never for a role, and answers 403 without it; page 13 shows
where the check sits, page 14 the tables. The database roles `gea_app` and `gea_relay` on page 13 are logins of the services, not roles of users.

## The main rule

**Core phase: Bronze input → Core Calculation → immutable Silver.**

**Custom phase: user selects a process → Silver + permitted input overrides
→ custom calculation → versioned Gold.**

Bronze is the incoming data layer. The first calculation phase is **Core**:
Python retrieves the parameter definitions, source mappings, validation and
Core rules from a published **DataContract**. It extracts and maps the Bronze
values into the calculation model and has Snowflake execute the Core rules.
A complete successful output becomes Silver.
The original Bronze snapshot is retained for lineage.

After Core succeeds, the user can select **any published custom process**—for
example Regional Calculation—and create or edit that process's scenario.
For each new custom run, Python loads **both DataContract and Custom
Configuration** using their selected revisions. These reads are independent;
neither document must be read to discover the other. Python waits for both,
checks that the configuration is compatible with the contract and source
model, and builds one execution plan. The configuration selects the process,
formula bindings, constants and permitted input overrides. An override replaces
the selected parameter in this run's assembled input; unoverridden parameters
come from the source model. DataContract defines the parameter structure,
defaults and limits on allowed customization. Silver itself never changes. Every successful
run delivers a complete new Gold model. After each delivery the user decides
how to continue: **finish the pipeline**, **edit the scenario and rerun from
Silver**, or **process the delivered Gold further** by chaining a new scenario
that explicitly pins that Gold as its source. Each iteration appends another
Gold model. The mutable object is the scenario/head, with retained history.
Gold result snapshots are immutable so past calculations remain reproducible.

In the proposed API, the two phase values are `core` and `custom`.
A custom command selects `processName` and a separate
`customConfiguration: {configId, revision}` reference, which must match its
scenario. The selected configuration defines `Regional Calculation` in the
worked example and must satisfy the selected DataContract. Another compatible
configuration can define another process. Earlier references to regional scenarios,
bindings or profiles describe that example of the generic custom phase.

These are logical layers, not a requirement for three databases, three copies
of every intermediate value, or three microservices.

## Responsibilities and connections

### An assessment in business terms

**The Python worker coordinates the calculation. Snowflake executes the formula.
The application displays the saved result or failure.**

Use these full business names throughout the example:

| Model or request | Example and meaning |
|---|---|
| Original policy model — Bronze | `{ DeathPenatly: 1000 }`, retained as submitted |
| Immutable core model — Silver | Core rule caps the amount at 800, producing `{ DeathPenatly: 800 }` |
| Editable custom scenario — Regional example | The user overrides this run's input to `{ DeathPenatly: 700 }`; the core model remains 800 |
| Regional calculation request | A saved instruction to calculate that exact regional scenario using the selected rules |
| Completed regional model — Gold | Regional rule reduces the selected amount by 10%, producing `{ DeathPenatly: 630 }` |

These are illustrative rules for the user's requested property, whose spelling
`DeathPenatly` is preserved. Diagram amounts use short business notation; the
API continues to transport decimal amounts as strings. The example is a
proposed one-property contract, separate from the existing insurance fixtures.

First, **Core Calculation** transforms Bronze `{ DeathPenatly: 1000 }` into
Silver `{ DeathPenatly: 800 }`: DataContract identifies `DeathPenatly`, maps
its Bronze value, validates it as a decimal, and supplies the Core cap of 800.
Once that completes, the user selects a custom configuration, here
**Regional Calculation**, with an input override of 700 and factor 0.90.

The user clicks **Calculate** for that custom process. Python independently
loads its DataContract and Custom Configuration, validates both against the
source model, and freezes their revisions and hashes with the assembled input
and execution plan. The application saves the request and returns a calculation reference. The screen shows
**Waiting**. An available background worker picks up the request, loads the
saved input and asks Snowflake to calculate `700 × 0.90`. Snowflake returns
`630`. The worker saves the complete Gold model and **Ready** status together.
The application then loads that model for the user.

A later user edit to `{ DeathPenatly: 600 }` creates a new scenario revision
and a new calculation. The same regional rule produces `{ DeathPenatly: 540 }`.
The immutable core stays 800 and the earlier completed regional result stays
630. A retry of an existing calculation uses its original saved input; it
does not pick up that later edit.

### Where the Data Contract participates

The [one-property contract example](death-penatly-contract.example.yaml)
defines the model and permitted customization. It is an illustrative published
version 1 of `DEATH_PENALTY_EXAMPLE`, with:

- Required property `DeathPenatly`, its Bronze source mapping and nonnegative decimal validation.
- Core rule: cap the value at `800.00`, using an exact released SQL function.
- Custom-phase defaults and permission to replace the input value while
  preserving the original core model.

The separate [Custom Configuration example](custom-configuration.example.yaml)
selects **Regional Calculation**, the released regional adjustment function,
factor `0.90` and input override `{ DeathPenatly: 700 }`. Those choices are
configuration data, not the DataContract's definition of the model.

The application first checks whether the command was already accepted. For a
Core command, **Python reads DataContract to discover and map the Bronze
parameters before calculating Silver**. For a new custom command, **Python
loads both the selected DataContract and selected Custom Configuration from
Snowflake independently**. The command supplies both references. Python joins
the two results with the source model, verifies hashes and compatibility,
checks permitted edits and function signatures, and freezes one effective
input and execution plan with both document identities, revisions and hashes.
The worker later loads that saved plan. Snowflake stores the contract and
executes the SQL functions; Python interprets the contract.

The diagram begins after publication of the contract and configuration.
Before publishing the custom configuration, the illustrative
`REGIONAL_ADJUST_AMOUNT` SQL function must also be published. Neither these
example documents nor that function are currently
seeded in the running application. The existing four-parameter fixture
contract remains separate.

### Which technology provides each part?

The business name describes a responsibility; the technology below specifies
where it runs. **The cloud deployment is proposed, not already implemented.**

| Business responsibility | Concrete technology and location | Present in this repository? |
|---|---|---|
| Calculation application | Litestar Python web service; proposed deployment in its own container on Amazon Elastic Container Service with AWS Fargate | Local web service exists; cloud deployment and asynchronous run endpoints do not |
| Saved models, requests and status | Snowflake database tables | Local emulator stores models and audit; new durable job/status tables are still required |
| Background job waiting list | An Amazon Simple Queue Service **First-In, First-Out** queue hosted in AWS, separate from Python and Snowflake | Proposed; neither an AWS queue nor a local queue emulator is configured |
| Delivery process, previously called the relay | A separate Python process reads saved, undelivered calculation requests from Snowflake and sends their references to the Amazon queue using the boto3 AWS SDK | Proposed; the existing audit exporter is a similar delivery pattern but copies audit events to PostgreSQL |
| Background calculation worker | Our Python program in a separate container, kept running by an Amazon Elastic Container Service service on AWS Fargate | The `CalculationWorker` class exists; its queue-consuming process and service deployment do not |
| Policy formula execution | Released SQL functions inside Snowflake; locally, the Snowflake emulator executes supported formulas in DuckDB | Implemented locally |
| Screen updates | Vue asks the Litestar web service for the saved calculation status and loads the completed model | Proposed for the asynchronous run API |

**Amazon Simple Queue Service is the waiting list.** It stores messages
containing calculation references. The Python worker calls boto3
`receive_message()` with long polling to wait for available work. It receives
a message when available, loads the full saved request from Snowflake and
processes it. The queue does not execute Python or insurance formulas.
[Amazon queue consumption](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html),
[boto3 receive_message](https://docs.aws.amazon.com/boto3/latest/reference/services/sqs/client/receive_message.html).

The delivery process is application code, not an additional AWS product. It
sends only requests already saved successfully in Snowflake, and marks their
delivery after the queue acknowledges receipt. This avoids losing accepted
calculations if the API stops between saving the request and handing it to
the queue. Duplicate delivery is handled through saved job ownership and
result checks; the technical rules are on page 8.

### What the worker looks like

**Worker describes the job of an ordinary Python program.** It is not a
special Python language feature or a scheduler supplied by the existing
class. The proposed program has a receive-and-process loop:

```text
Wait for a calculation reference from Amazon Simple Queue Service
  → claim the saved request and mark it Calculating
  → load its fixed model, scenario and selected rule versions
  → call the existing CalculationWorker.run() calculation engine
  → ask Snowflake to execute the selected SQL functions
  → save the complete result and Ready, or save a failure/retry outcome
  → acknowledge a completed queue message; continue waiting for work
```

This describes the control flow, not executable production code. Ownership,
lease renewal, rollback, bounded retry and recovery are required around the
calculation call, as detailed in the orchestration section.

The real class is in [calculation_api/worker.py](../calculation_api/worker.py).
Its `run()` method validates inputs, groups parameter calls in dependency
order, calls the database and gathers returned values and audit evidence.
[SnowflakeGateway.execute_batch()](../calculation_api/gateway.py) sends the
formula calls through the Snowflake connector. **Today this class runs inside
the web request. It does not listen to a queue.** The separate program would
wrap this existing engine; it still needs implementation.

Amazon Elastic Container Service maintains the configured worker containers
and replaces stopped tasks; AWS Fargate supplies their compute. One worker
can process many successive jobs. Keeping at least one worker service
instance available allows it to wait for new requests. Closing a browser does
not stop that service. [Amazon container service behavior](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html).

### How the client learns that work finished or failed

The first response means **Accepted**, not **Completed**. It gives the screen
a calculation reference. Invalid submissions are rejected before acceptance.
The screen then makes short automatic status checks for that same request
while the worker runs independently. These checks read saved state; they do
not start another calculation.

| Screen message | What it means | Saved status |
|---|---|---|
| Waiting | Accepted and waiting for a worker | `QUEUED` |
| Calculating | A worker is processing the saved model and rules | `RUNNING` |
| Retrying | A temporary problem is being retried within the allowed limit | `RETRYABLE` |
| Ready | The complete Gold model is saved; load `{ DeathPenatly: 630 }` | `SUCCEEDED` |
| Failed | Show the saved reason and an allowed retry or scenario edit | `FAILED` |

The status check returns a result reference on success; the screen retrieves
the saved model through the application. It stops checking at Ready or
Failed. A temporary browser connection problem means status is unavailable,
not that the calculation failed. On reopening, the screen reads the saved
calculation reference and its latest outcome.

The initial client update mechanism remains automatic status checks
(polling). Server-Sent Events could later provide server push over a browser
connection, with saved status still used for recovery. Page 11 explicitly
shows the chosen polling flow. EventBridge is for downstream system events;
it is not the browser update mechanism here. On a terminal failure, no
incomplete Gold model is published and previous completed models remain
available. A recovery service handles abandoned jobs so a crashed worker does
not leave the request permanently marked Calculating.

### What the Retry calculation button does

[Page 12](12-calculation-retry-sequence.svg) shows automatic and manual retry
as messages between the same participants used by the main sequence.

| Situation or action | Required behavior |
|---|---|
| A temporary execution problem occurs | Roll back incomplete output, retain the failed attempt and show Retrying. A bounded automatic retry reloads the same saved input and plan. |
| The problem is permanent or the automatic allowance is exhausted | Save Failed with an explanation and whether a manual retry is allowed. Preserve earlier models. |
| User clicks **Retry calculation** | Send `POST /runs/{runId}/retry` with a request ID. For an eligible failed calculation, save a new dispatch generation and bounded retry allowance against the same calculation, then return 202. |
| A second click or network replay repeats the retry command | Return the saved command response; do not create another competing attempt. |
| The calculation is already queued, calculating or retrying | Return the existing active work; no new attempt is launched by the duplicate command. |
| The calculation has already succeeded | Return the saved Gold result; no recalculation. |
| User edits the amount to 600 or selects another DataContract or Custom Configuration revision | Create a new scenario revision/calculation. This is not a retry of the original request. |

For the example, retry keeps `{ DeathPenatly: 700 }`, DataContract version 1,
Custom Configuration `REGIONAL_DEATH_PENALTY` revision 1, and its regional
factor `0.90`. Success still produces `{ DeathPenatly: 630 }`, even if either
document has a newer published revision. A retry loads the frozen run; each
new custom calculation independently loads both selected documents again.
The Retry calculation action is offered only when the server reports that
the saved failure is eligible. The client resumes checking the same
calculation reference and shows its new attempt's outcome.

### Detailed module responsibilities

| Layer/module | What it owns | How it connects |
|---|---|---|
| Vue workspace | Input submission, scenario edits, run/retry actions, results and audit views | HTTP to Litestar; reads API types generated from OpenAPI |
| Litestar command API | Validate commands, resolve exact revisions, freeze plans and create durable runs | Commits run/receipt/dispatch outbox in Snowflake; returns 202; exposes run status |
| Data Contract module | Parameter schema, Bronze source mappings, Core bindings, custom defaults, dependencies, objectives and allowed customization | Python reads the selected published revision; for custom runs this is an independent input alongside Custom Configuration |
| Custom Configuration catalogue | Process name, exact custom function bindings, constants and permitted parameter overrides | A separate document in `CONFIG_REVISIONS`; Python loads its selected revision independently and validates it against DataContract before freezing the plan |
| AWS run orchestration (target) | Durable dispatch, execution, retry and recovery | RUN_OUTBOX relay → SQS FIFO → separate ECS Fargate worker; Snowflake owns run status and fenced leases |
| Bronze | Original input snapshot, source identity, received time and hash | Source for Core Calculation; retained even when a core run fails |
| Core Calculation | Discover parameter definitions and source mappings from DataContract, model Bronze input and execute Core DB functions | Reads Bronze plus DataContract; writes Silver only after complete success |
| Silver | Immutable Core output and lineage to Bronze, contract and functions | Read-only source for independent custom scenarios |
| Custom workspace | Selected process, pinned source, DataContract and Custom Configuration references, and draft revision | Saves user edits as a versioned configuration/overlay and selects both documents for each new run |
| Custom Calculation | Apply the configured process, such as Regional Calculation; preserve other assembled input values | Independently load DataContract and Custom Configuration, join them with the source model, validate and freeze; use the same worker and SQL execution mechanism as Core |
| Gold | Complete custom output for a specific successful run, with lineage and objective | Read/compare/export; scenario head may point to the latest successful result |
| DB adapter and Snowflake/emulator | Execute released SQL functions, return values/query IDs, persist models and audit | Runtime calls the adapter; functions execute in the database |
| Run/audit records | Frozen run manifest, attempts, status, before/after values, query traces | Result, success audit and outbox commit together; the PostgreSQL audit copy stays downstream |

For local execution, the DB adapter continues to use the real Snowflake Python
connector against the emulator, which executes supported SQL in DuckDB. The
existing worker remains reusable. A separate Data Contract **module** can live
in the same backend process; a new network service is not required.

## What the Data Contract specifies

This is a **domain calculation contract**. OpenAPI remains the HTTP contract
between Vue and Python. They describe different boundaries.

The contract is an abstract, storage-agnostic specification; its published
revisions are persisted in Snowflake (locally, the emulator), next to the
function catalogue and the models they govern. The Bronze → Silver run reads
its pinned contract revision from the same database that executes the
functions, so the first data model transformation is driven entirely by data
read from Snowflake.

The Data Contract contains:

- A stable contract ID, immutable revision and content hash.
- Parameter names, Bronze source mappings, types, precision, units where
  applicable, requiredness and validation. Arbitrary model parameter sets are supported by publishing a
  matching contract; there is no fixed list in the runtime.
- Core parameter bindings: exact `functionId` and revision, named argument
  mappings, typed constants, dependency order and output rules.
- Core completeness rules and custom-process copy-through rules.
- Custom defaults and allowed parameter, binding and constant overrides.
- A versioned objective function, its baseline and DB aggregation rule.

[data-contract.example.yaml](data-contract.example.yaml) specifies all four
example parameters. It is a proposed domain format, not an accepted payload
for the current v2 endpoints.

Both contract examples use `phases.core` and `phases.custom`. A separate
Custom Configuration selects the process name, custom function bindings,
constants and parameter overrides within the contract's permissions. The
one-property [configuration](custom-configuration.example.yaml) and
[four-parameter configuration](insurance-custom-configuration.example.yaml)
are separate stored documents. Publishing a compatible configuration can
introduce another process without adding a hard-coded phase to the API.

Argument sources are explicit: `phaseInput` is the frozen input for this run;
`phaseOutput` is an already-calculated parameter from this same phase;
`constant` is a typed value frozen into the effective plan. A `phaseOutput`
reference must appear in the dependency graph. For example, core
`CriticalIllness` reads the **calculated** core `Death`, not an accidental mix
of raw and updated values. Cycles and unresolved references fail before DB
execution.

Function formulas remain editable in the SQL function catalogue. Publishing
creates immutable releases. A contract or scenario can reuse a core release
or select a compatible custom SQL release for a region. The resolver verifies
the function signature and records its SQL name and definition hash. Python
does not execute the formula, and the frontend does not supply an arbitrary
SQL identifier to the worker.

After both independent document reads have completed, Python constructs the
effective custom plan with this precedence:

1. Custom defaults from the pinned DataContract.
2. The selected Custom Configuration's bindings, constants and parameter
   overrides, subject to DataContract permissions.

The resolved result is validated and frozen. A region label alone does not
select a floating configuration. Merge precedence is not read order: the
configuration read does not depend on the contract read. Function, contract
and configuration revisions can change without changing any already-created run.

## Contract storage and publication (target)

**Snowflake is the runtime source of truth; Python interprets the contract.**
The database stores the document and executes released SQL functions. It does
not interpret phase bindings or dependency graphs in this design. A future
Snowpark procedure could read the same document, but that is a separate change.

| Option | Assessment |
|---|---|
| Contract supplied in each Vue request | Suitable for draft preview; a production run must reference a server-published revision with verified provenance |
| Separate RDS or S3 store | Possible, but publication must coordinate contract references with Snowflake function releases across stores; consider only with a concrete separate-consumer or authoring requirement |
| Published revisions in Snowflake | Selected target: contract, function-release registry, models and lineage share a database and publication can validate bindings within its transaction |

Proposed tables (not yet in `fixtures/lifecycle.sql`):

| Table | Stored state |
|---|---|
| `INSURANCE.CONTRACTS.CONTRACT_DRAFTS` | Mutable document per `CONTRACT_ID`, optimistic `REVISION`, editor and edit time; `expectedRevision` guards edits and publication |
| `INSURANCE.CONTRACTS.CONTRACT_REVISIONS` | Immutable `(CONTRACT_ID, REVISION)`, `CONTENT_HASH`, `DOCUMENT VARIANT`, `PUBLISHED_AT`, `PUBLISHED_BY` |
| `INSURANCE.CONTRACTS.CONTRACT_STATUS` | Separate lifecycle metadata for published/deprecated revisions; deprecation does not mutate a published document/hash or invalidate a frozen retry |

Custom Configuration is stored separately in the existing
`CONFIG_REVISIONS.DOCUMENT_JSON` pattern. Its proposed document shape adds the
process name, compatible contract reference, custom bindings/constants and
permitted parameter overrides. `ConfigCatalog` and immutable configuration
revisions already exist; this revision 3 shape and combined resolver do not.
Both documents live in Snowflake and are separate inputs, not separate cloud
services. A custom command names both references, so Python can load them
independently and join their results before validation and plan creation.

Publication validates parameter schema, required core coverage, allowed
overrides, argument types/sources, acyclic dependencies, and the existence and
signatures of all exact `functionId@revision` references, including the
objective, in `FUNCTION_RELEASES`. Insert the immutable revision only after
validation succeeds, with revision uniqueness enforced. SQL function release
DDL is completed beforehand; do not place `CREATE FUNCTION` inside this
contract-publication transaction. Existing `CONFIG_REVISIONS.DOCUMENT_JSON`
is the reusable storage pattern, not an already implemented contract table.

At Core command acceptance, Python reads the exact contract revision to obtain
parameter definitions, source mappings and Core rules. For every new custom
run, Python loads both the contract and Custom Configuration revisions,
independently, and checks their compatibility with the source model. It
verifies canonical document hashes, resolves the plan and saves both document
identities/revisions/hashes, the frozen input and `planHash` in `RUNS`.
If the caller requests `latestPublished` for the contract, resolve it once
after checking the command receipt and pin the resulting revision. Replaying
the same request must return the original run even after a new publication.
At worker start and retry, load the frozen plan and input by run ID and verify
their hashes; do not resolve a newer contract, configuration, draft or release.

An optional process-local LRU uses `(contractId, revision, contentHash)` as its
key. Mutable draft/status checks are outside that cache. This avoids repeated
parsing and resolution; no separate cache service is required. Warehouse
reads still have latency and may incur compute/resume charges; measure them.
Git/YAML may be the authoring source: CI calls draft edit and publish APIs,
while Snowflake remains authoritative at runtime. Prefer an AWS region shared
with the Snowflake account, key-pair credentials in Secrets Manager and
PrivateLink when required and supported by the deployment.

## AWS run orchestration (target, page 8)

### Stage 1: SQS FIFO and ECS Fargate

Split the Litestar command API, outbox relay and calculation worker into
separate processes. Reuse the formula-free `CalculationWorker` and connector.
The current `audit_export.py` demonstrates polling a committed SQL outbox and
acknowledging only after delivery; it does **not** currently dispatch runs to
SQS or publish EventBridge events.

| Record | Required contents and responsibility |
|---|---|
| `INSURANCE.CALC.RUNS` | `runId`, phase, pinned source/scenario/contract identities and hashes, frozen input/plan, `planHash`, status, attempt/retry budget, lease owner/expiry/token, dispatch generation, result reference |
| `INSURANCE.CALC.RUN_ATTEMPTS` | Attempt ID, run ID, lease token, start/end, outcome, error and query diagnostics; attempt start survives calculation rollback |
| `INSURANCE.CALC.RUN_OUTBOX` | Stable dispatch ID, run ID, generation, message group, delivery state; created with the run or a deliberate retry |
| Command receipts | Unique command scope/request ID plus payload hash and saved run ID, committed with acceptance; conflicting payload returns 409 |
| `AUDIT_EVENTS` + `AUDIT_OUTBOX` | Authoritative committed audit and independent delivery state of the PostgreSQL copy (`calc."AuditEvent"`), retaining the existing exporter pattern |
| `INSURANCE.CALC.RUN_EVENT_OUTBOX` | Stable completion event ID and compact result reference for EventBridge; independent delivery state from the audit exporter |

1. `POST /runs` checks the receipt, validates `expectedRevision` for regional
   scenarios, pins the source/contract and freezes the plan. Commit `RUNS`
   (`QUEUED`), receipt and `RUN_OUTBOX` together, then return **202** with
   `runId` and a `Location` status URL. Persist Bronze independently so a
   rejected or failed core calculation does not erase its source.
2. The relay sends `{runId, dispatchId, generation}` to SQS FIFO, and only
   marks that outbox row delivered after SQS acknowledges it. Use
   `MessageGroupId=scenarioId` for regional work and `bronzeId` for core work.
   Use `MessageDeduplicationId=dispatchId`: relay retries reuse it; explicit
   retry/recovery dispatches get a new ID and generation even for the same run.
   SQS deduplicates sends within a five-minute window; persisted run/attempt
   checks must handle later duplicates. [AWS FIFO deduplication](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-exactly-once-processing.html).
3. A worker conditionally claims an eligible `QUEUED`/`RETRYABLE` run for the
   message generation and commits `RUNNING`, a fresh fencing token, lease and
   attempt-start record. Serialize claims with a proven database concurrency
   mechanism. A zero-row claim requires inspecting current state: acknowledge
   obsolete generations or terminal runs; defer a message whose run is still
   owned by a live worker. Do not interpret every failed claim as completion.
4. Execute synchronous batched SQL calls from the saved plan. Heartbeat the
   database lease through a separate control connection and extend SQS
   visibility while SQL is running. Abort publication if ownership is lost.
   The current batch limit is 256 invocations, not a guarantee that a run
   finishes in seconds. SQS visibility is bounded to 12 hours from receipt;
   longer work needs smaller activities or Stage 2 orchestration.
   [AWS visibility guidance](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html).
5. In one result transaction, revalidate the fencing token/lease and commit
   complete Silver or Gold, lineage, traces, success audit, both downstream
   outboxes, attempt success and `RUNS.status=SUCCEEDED`. An affected-row check
   on run ownership must succeed or the transaction rolls back. Move the
   scenario result pointer only if its draft revision and selected run still
   match. An older valid run may retain a historical result without moving
   that pointer. Acknowledge the queue message only after commit.
6. On failure, roll back result writes, then persist attempt diagnostics and
   `RETRYABLE` or `FAILED` with an ownership check. Transient retries normally
   use visibility-based redelivery/backoff and an explicit persisted attempt
   budget. `maxReceiveCount` bounds queue deliveries and sends poison messages
   to a DLQ; it is not the calculation-attempt counter. A reaper fences expired
   owners and creates recovery dispatches transactionally when needed. A DLQ
   reconciler records terminal failure for still-unfinished runs, so polling
   cannot stay `RUNNING` indefinitely. Re-enqueueing must not reset the stored
   retry budget. Manual retry of a failed run creates a new dispatch against
   the same frozen plan with a separately authorized retry budget.

FIFO orders messages as sent within a group. It does not establish database
command order across relays, guard scenario edits, or prevent a late worker
from committing. Fencing and conditional result-pointer updates remain
mandatory. The deployment must prove uniqueness and atomic ownership under
concurrent sessions: ordinary Snowflake table primary/foreign keys do not
provide the enforced uniqueness of hybrid tables. The preferred candidate is
hybrid control tables in the same database with transactional result writes;
validate supported data types (keep large document payloads in standard tables
if necessary), availability and contention before implementation. If an
external ownership store is chosen instead, redesign the cross-store commit
protocol rather than claiming it is one Snowflake transaction.
[Snowflake hybrid constraints](https://docs.snowflake.com/en/sql-reference/sql/create-hybrid-table),
[cross-table transactions](https://docs.snowflake.com/en/user-guide/tutorials/getting-started-with-hybrid-tables-tutorial),
[hybrid limitations](https://docs.snowflake.com/en/user-guide/tables-hybrid-limitations).

### Results, audit and completion events

`RUNS` is authoritative for `GET /runs/{runId}`. Vue starts with polling and
`ETag`/`If-None-Match`; SSE can be added later using the same persisted state.
Success returns an immutable Silver/Gold reference; failures expose attempt
diagnostics and retry eligibility. Never infer success from queue deletion or
an EventBridge notification.

The existing audit path remains `AUDIT_OUTBOX → audit-export → PostgreSQL`.
For optional notifications/downstream consumers, a separate completion relay
reads `RUN_EVENT_OUTBOX` and publishes `RunCompleted` to EventBridge after the
result commits. Check each `PutEvents` entry before acknowledging delivery;
HTTP 200 can include failed entries. Consumers deduplicate by the stable event
ID and fetch authoritative results by reference. Use independent per-sink
delivery state if the audit projection later moves behind EventBridge; the
single existing `AUDIT_OUTBOX.DELIVERED` flag cannot acknowledge two sinks.
[EventBridge PutEvents response](https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutEvents.html).

### Stage 2: durable multi-step workflows

Add an engine when Core → human decision → regional fan-out → explicit Gold
chaining must be one persisted workflow. AWS Step Functions **Standard** is
the AWS-oriented option, with service integrations and callback waits;
Temporal is an alternative for workflow code and cloud portability. Keep
callback tokens server-side and resume only through authorized API commands.
Both would coordinate the same idempotent run API and frozen results; choose
one owner for each retry layer to avoid multiplied retries.
[Step Functions integration patterns](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html).

Celery or another Python task queue can be used, but does not remove the need
for authoritative run state and result publication rules. Snowflake Tasks or
Snowpark would move orchestration into the database and require a distinct
design decision. This target uses Fargate worker processes and synchronous
connector calls; no Hangfire dependency, Celery result backend or
`execute_async()` query listener is required. Scheduling, if later needed,
should submit the same idempotent commands rather than bypass run acceptance.

## How the calculation runs

1. Save a Bronze input snapshot and select an exact DataContract revision.
   Python reads its parameter definitions, source mappings, validation and
   Core bindings from Snowflake, then extracts and maps the Bronze values.
2. Validate the modeled input and core bindings; resolve all referenced SQL releases
   and freeze the execution plan. Atomically persist the command receipt,
   `RUNS` manifest and `RUN_OUTBOX`; return 202 with a run ID. The relay sends
   the run to SQS and a separate worker claims a fenced lease (page 8).
3. The worker loads the saved plan, verifies its hash, maps arguments and batches calls by dependency wave and function
   release. The DB executes the formulas and objective. It returns values and
   query IDs; the runtime assembles all output parameters.
4. Commit the complete Silver snapshot with source/contract/function lineage,
   success audit/outboxes and `SUCCEEDED` status. Failure creates no Silver output; retain Bronze
   and the failed run attempt.
5. After Core succeeds, the user selects a Custom Configuration and creates
   a scenario referencing Silver. Save permitted input overrides with the
   configuration and keep revision history.
6. For every new custom run, independently load the selected DataContract and
   Custom Configuration. Once both are available, validate compatibility with
   the source model and assemble `Silver snapshot + permitted parameter overrides`.
   Combine contract defaults with the configuration's bindings and constants,
   freeze both documents and the plan, and invoke the
   same asynchronous dispatch and calculation runtime. Parameters without a custom binding are copied
   from this assembled input, including valid explicit user edits.
7. Commit a new Gold snapshot plus audit/outboxes and `SUCCEEDED` status. Preserve Silver and previous
   Gold snapshots. Record scenario revision, source hashes and both document
   revisions/hashes in Gold lineage.
8. Vue polls `GET /runs/{runId}` to discover the committed result (SSE is optional).
   Deliver the Gold model to the user, who decides how to continue: finish the
   pipeline, edit the scenario and rerun from Silver (steps 5–7), or process
   the delivered Gold further by creating a new scenario with
   `sourceMode: gold` that pins this Gold ID/revision/hash (see explicit
   chaining below). Every iteration delivers another versioned Gold model.

Persist the run manifest before attempting calculation so failed attempts can
still be inspected. Commit output and success audit atomically. After rollback,
record failure status/diagnostics separately; never publish partial Silver or
Gold output. Concurrent writes require conditional scenario revision/run
ownership checks. A stale run must not overwrite a newer scenario's result
pointer; its successful snapshot can remain in history for the draft it used.

## Edit, rerun and retry mean different things

| User action | Input/version rule | Result |
|---|---|---|
| Edit a scenario | New draft revision; Silver unchanged | No new Gold until calculation succeeds |
| Calculate or explicitly rerun | New run, freshly assembled from the pinned Silver plus the chosen draft | New Gold snapshot, even if the values happen to match an earlier run |
| Repeat the same submit command after a network timeout | Same request ID and same payload | Return the existing run/result; do not create another run |
| Retry a failed run | An idempotent retry command targets the same run, frozen model, DataContract, Custom Configuration and plan; create a new attempt | Publish Silver for Core or Gold for custom only after success |
| Retry an already successful run | Read the saved run result | Return its existing Silver or Gold snapshot |
| Retry while the run is active | Read the existing run status | Do not start a competing attempt |
| Change inputs, constants or function version | New scenario revision and new run | Old results retain the earlier configuration |

A new request using an old idempotency key with a different payload is a
conflict. Retrying must not read a newly edited draft or silently select the
latest contract, Custom Configuration or function release.

The default regional source is Silver. Earlier requirements for sequential
regional processes remain available as **explicit chaining**: create a new
scenario with `sourceMode: gold` and pin the parent Gold ID/revision/hash. Its
own repeated runs rebuild from that pinned parent plus its overlay. Preserve
the original Silver lineage and objective baseline. Never silently use the
previous Gold output as the input of a normal Silver-based rerun.

## Concrete example

[Page 9](09-parameter-layer-sequence.svg) shows the two-phase actor sequence;
[page 3](03-silver-to-gold.svg) expands custom scenarios. Both use the same one-property
example: original `{ DeathPenatly: 1000 }` → immutable core
`{ DeathPenatly: 800 }` → user-edited regional input
`{ DeathPenatly: 700 }` → completed Gold `{ DeathPenatly: 630 }`.
The core rule caps at 800 and the regional rule reduces the selected amount
by 10%. A later edit to 600 produces a new completed result of 540, while
retaining the original core and earlier Gold result. These examples use full
model names, rather than shortened model/request labels.

[Page 10](10-assessment-worker.svg) maps business responsibilities to the
actual technologies. [Page 11](11-assessment-client-updates.svg) shows Data
Contract and Custom Configuration loading, request acceptance, background processing, stored
success/failure and client updates. [Page 12](12-calculation-retry-sequence.svg)
shows automatic retries and the user's Retry calculation action, preserving
the original input, contract and configuration. Both use the
[one-property contract](death-penatly-contract.example.yaml) and its separate
[Custom Configuration](custom-configuration.example.yaml).
The following four-parameter example is retained as an integration reference;
it uses different fixture rules from the simplified business example.

Illustrative integration values, in the same amount unit:

| Parameter | Bronze input | Silver after Core Calculation | First Gold result, first scenario | Second Gold result, edited scenario |
|---|---:|---:|---:|---:|
| Death | 250000.00 | 250000.00 | 200000.00 | 180000.00 |
| AccidentalDeath | 300000.00 | 250000.00 | 150000.00 | 150000.00 |
| TotalPermanentDisability | 200000.00 | 200000.00 | 200000.00 | 200000.00 |
| CriticalIllness | 150000.00 | 125000.00 | 112500.00 | 100000.00 |

Core uses `CORE_LIMIT_AMOUNT_R1` for Death and `CORE_LIMIT_RELATIVE_R1` for the
other parameters. The first regional scenario caps Death at 200000 and AccidentalDeath at
150000, leaves TotalPermanentDisability unbound, and maps CriticalIllness to
custom release `CUSTOM_CI_R1(AMOUNT, FACTOR)` whose SQL body is
`ROUND(AMOUNT * FACTOR, 2)`.

The first regional scenario passes `AMOUNT=125000.00`, `FACTOR=0.90`, producing `112500.00`. The user
then creates a new scenario revision by changing the Death cap to 180000 and the factor to 0.80.
The edited scenario again passes Silver's `125000.00`, producing `100000.00`. It does not use
the earlier Gold result of `112500.00`, which would incorrectly produce `90000.00` for this intended
Silver-based scenario. Replaying the second request returns its saved Gold result without another transformation.
The Bronze and Silver columns are the same fixture values that
[../FLOW.md](../FLOW.md) walks through against the implemented endpoints.

## Scale and storage choices

Keep orchestration separate from formulas. Resolve and cache published
contracts/releases by exact revision/hash, then load one frozen plan per run;
avoid catalogue round trips for each parameter. Continue grouping independent
calls by function release and dependency wave. The existing local worker uses
256 invocations per transformation query and SQL objective aggregation; the
configurable limits are documented in
[../README.md — Scale and safety boundaries](../README.md#scale-and-safety-boundaries).

Store scenario edits as overlays. Send patches from Vue and page large model
views; a full model should not travel back and forth just to change one value.
For larger inputs, stage model rows in the database and use set-based function
queries. Every published output remains logically complete, even if storage
later uses references to unchanged values.

The current API serializes work inside one process. SQS FIFO, separate Fargate
workers, fenced leases and outbox dispatch are the explicit Stage 1 target
above; they are not implemented locally. No production throughput guarantee
follows from emulator tests. The audit export to PostgreSQL remains
asynchronous to the committed SQL result and is not a calculation dependency.

## Implemented runtime (pages 4–7)

The runtime below is what exists today and what the target layers reuse. Its
behavioral rules are specified in
[../REQUIREMENTS.md](../REQUIREMENTS.md#implemented-version-2-lifecycle-rules).

### Process boundaries

The default Compose stack has `calculation-api` on port 8000 and
`snowflake-emulator` on port 8084. `ModelLifecycle`, both catalogue classes,
`CalculationWorker`, `Repository`, `SnowflakeGateway`, and the Snowflake Python
connector run inside the API process. The worker is a Python class invoked by
the request; it is not a separate queue consumer. The API serializes its
database operations using a process-local lock.

The emulator is a separate HTTP service. `Engine.compile()` maps supported
Snowflake SQL to DuckDB SQL. Registered scalar SQL UDFs become typed DuckDB
SQL macros. DuckDB executes the formulas and stores the catalogue, models,
revisions and audit tables. The compose volume stores the database file and
query-history JSONL file. This is the local Snowflake substitute, accessed
through the real Snowflake Python connector.

The optional `audit` profile adds a separate `audit-export` process and the
PostgreSQL of the GEA module. That exporter has its own Snowflake connector
session, reads committed outbox records, and inserts events with psycopg.
There is no real cloud Snowflake/AWS deployment, queue or distributed scheduler
in the current package.

The package contains a Vue `openapi-fetch` client and composable example.
`../api/openapi.yaml` is manually maintained and served at `GET /openapi.yaml`.
The example imports `generated.ts`, but that generated file and a runnable Vue
application are not currently included; type generation is a documented build
step, represented by dashed connectors on page 4. The backend route handlers
accept dictionaries and perform manual validation; the YAML is not the
automatic source of Python DTO classes.

### Function selection and calculation

1. `FunctionCatalog.edit()` persists a mutable draft definition and revision.
2. `preview()` creates a uniquely named preview SQL UDF, calls it through the
   connector, and drops it afterwards.
3. `publish()` creates `INSURANCE.RELEASES.<ID>_R<revision>` and inserts its
   immutable release metadata. The DDL precedes the registry insert; this is
   distinct from the model/audit transaction on pages 6 and 7.
4. `ConfigCatalog.resolve_bindings()` retrieves the published release and
   embeds its SQL name, signature, body and hash into each parameter binding.
   Configuration revisions and effective variation settings pin these records.
5. A new core reads `CONFIG_REVISIONS.DOCUMENT_JSON`. A variation calculation
   uses its stored effective settings. The worker does not fetch a Python
   callable, and it does not reread a mutable function draft per parameter.
6. `ModelLifecycle.calculate()` builds the stage, converts saved settings
   through `worker_bindings()`, then calls `CalculationWorker.run()`.
   The worker validates dependencies, groups ready parameter calls by SQL
   function, calls `execute_batch()`, merges the returned values by `CALL_ID`,
   requests the SQL objective, and writes calculation traces.
7. `Repository` saves the model/revision, an `AUDIT_EVENTS` payload,
   `AUDIT_OUTBOX`, and the idempotency receipt. The API commits this together
   with the worker's calculation trace using the same connection.

For the seeded core, Death uses `CORE_LIMIT_AMOUNT_R1`; the dependent
parameters use `CORE_LIMIT_RELATIVE_R1`. Regional caps use `REGIONAL_CAP_R1`.
All are under `INSURANCE.RELEASES`. Page 5's `CUSTOM_CI_R1` is the concrete
custom-function example exercised by integration tests, rather than a separate
service or a Python formula.

### Model history, audit and scale

The revision rules for core models, variation heads and pinned parents are
[REQUIREMENTS.md lifecycle rules 1–4](../REQUIREMENTS.md#implemented-version-2-lifecycle-rules);
audit durability and the PostgreSQL copy are in
[README.md — Audit storage](../README.md#audit-storage). Two details are only
visible in the diagrams:

- Page 7: failed calculations roll back their model/audit/outbox writes, while
  the emulator query-history log still records the failed SQL.
- Page 6: the 100-policy integration case invokes 400 parameter
  transformations and 400 objective terms in five function-execution queries
  (objective aggregation groups up to 50 policies per query). This count
  excludes configuration reads, persistence, audit inserts and transaction
  statements.

## Changes required from the existing implementation

| Existing capability | Required next change |
|---|---|
| `FunctionCatalog` drafts/releases | Retain; expose validated releases to the Data Contract resolver |
| `ConfigCatalog` and saved bindings | Retain separate Custom Configuration revisions for process/bindings/constants/overrides; add DataContract parameter/source modeling and validate the independently loaded documents together |
| `CONFIG_REVISIONS.DOCUMENT_JSON` | Add `CONTRACT_DRAFTS`, immutable `CONTRACT_REVISIONS` and separate lifecycle status; publish against exact function releases and verify canonical hashes |
| `CORE_MODELS` | Treat as Silver; add an independently retained Bronze input record and source reference |
| Variation current values and revision history | Separate scenario input overlay from calculated Gold output |
| `ModelLifecycle.change(calculate=True)` calculates `document["results"]` | Assemble the pinned Silver/source snapshot plus the draft overlay for each new run; this is the key behavioral change |
| Successful command receipts | Add durable run manifests and attempt status for explicit failure retry and observability |
| In-process worker and request lock | Add `RUNS`, `RUN_ATTEMPTS`, `RUN_OUTBOX`; split API/relay/Fargate worker; implement claims, fencing, heartbeats, recovery and DLQ reconciliation |
| Direct audit outbox projection | Retain; add independent `RUN_EVENT_OUTBOX` and optional EventBridge relay for completion notifications |
| Saved variation results | Expose immutable Gold results linked to source, scenario revision and run; guard the current result pointer |
| Manual OpenAPI and Vue client examples | Implement the proposed contract/run API in `../api/openapi-v3-draft.yaml`, promote it into the served contract and regenerate types; add polling; a full Vue app is still future work |

Keep the current methods/tests until this revision is implemented deliberately.
Required future integration cases include Silver immutability, frozen-contract
retries, complete core output, untouched regional parameters, fresh-source
reruns, the 0.90/0.80 example, failed-run diagnostics without partial Gold,
duplicate command handling, stale edits, concurrent result-pointer protection,
explicit Gold chaining and audit recovery.
Also cover publication races, duplicate deliveries beyond the FIFO window,
explicit retry within that window, relay crashes after send, worker crashes
before/after commit, lost leases, delayed completions, persisted retry budgets,
DLQ reconciliation and independent downstream-outbox recovery.

## Source traceability

| Page | Source files checked |
|---|---|
| 1–3 (target) | [../REQUIREMENTS.md](../REQUIREMENTS.md), [data-contract.example.yaml](data-contract.example.yaml); example values verified with DuckDB SQL |
| 4 — Components | `../docker-compose.yml`, `../calculation_api/app.py`, `../snowflake_emulator/app.py`, `../snowflake_emulator/engine.py`, `../examples/vue/src/api/client.ts` |
| 5 — Functions and bindings | `../calculation_api/catalog.py`, `../calculation_api/bootstrap.py`, `../fixtures/functions.sql` |
| 6 — Calculation sequence | `../calculation_api/app.py`, `../calculation_api/lifecycle.py`, `../calculation_api/worker.py`, `../calculation_api/gateway.py` |
| 7 — Audit sequence | `../calculation_api/repository.py`, `../calculation_api/audit_export.py`, `../tests/test_audit.py`, `../gea/db/postgres/migrations/sql/0005_calculation_audit.up.sql` |
| 8 — Contract storage and AWS orchestration (target) | Storage/orchestration decisions above; `../api/openapi-v3-draft.yaml`; current patterns in `../calculation_api/catalog.py`, `../calculation_api/audit_export.py`, `../calculation_api/app.py`; linked AWS/Snowflake documentation |
| 9 — Core phase then custom runs | `death-penatly-contract.example.yaml`; Core cap 800, custom process Regional Calculation with user overrides 700 / 600 and 10% reduction; SQL arithmetic verified locally |
| 10 — Assessment worker (business view) | Business explanation above; `../calculation_api/worker.py` and `../calculation_api/gateway.py` for existing coordination/SQL execution; page 8 for proposed background service |
| 11 — Background execution detail with DataContract | `death-penatly-contract.example.yaml`; business status mapping above; `../api/openapi-v3-draft.yaml` for proposed acceptance, polling and failure; page 8 for durable dispatch and recovery |
| 12 — Automatic and user-requested retry | Frozen-input retry rules above; `death-penatly-contract.example.yaml`; `../api/openapi-v3-draft.yaml` for retry eligibility, request replay and active/successful run responses |
| 13 — GEA components | `../gea_api/routes.py`, `services.py` (`needs`), `auth.py`, `runconfig.py`, `queries.py`, `errors.py`, `db.py`; `../gea/spec/poc-data.json`; `../gea/frontend/src`; `../gea/db/postgres/migrations` |
| 14 — GEA data model | `../gea/db/postgres/migrations/sql/*.up.sql`; checked against the live catalogue with `../gea/tools/erd.py` |
| 15 — GEA wizard save | `../gea_api/services.py` (`update_run`), `../gea_api/runconfig.py`, trigger `"Run_BeforeUpdate"`, `../gea/frontend/src/composables/useRunWizard.ts`; exercised by `../tests/gea/test_api.py` |
| 16 — GEA submit and delivery | `../gea_api/services.py` (`submit_run`), triggers on `gea."DataContract"`, functions `gea."ClaimContractDeliveries"`, `"CompleteContractDelivery"`, `"RecordExecutionStatus"`, `"RecordExecutionLog"`; the relay itself is not implemented |
| 17 — GEA run execution | `../gea/db/postgres/migrations/sql/0006_jobs_and_execution_control.up.sql` (exercised by `../gea/db/postgres/tests/smoke.sql` groups 13 to 16), `../gea_api/services.py` (`create_job`, `get_run_execution`, `run_logs`, `cancel_run`, `resolve_run`); `../gea/db/snowflake/V001__contract_landing.sql`, `V002__run_pipeline.sql` (draft, never executed); the relay is not implemented |

## Regenerating the diagrams

```bash
python architecture/generate.py
```

The draw.io pages and the SVG previews are generated from the same scene
definitions in `generate.py`; edit the scene, not the outputs. The PNG previews are
not written by the script: render them again from the SVG files after a change,
otherwise they go stale (pages 9, 11 and 12 had, and were refreshed on 2 October 2026). Diagram checks
and the implementation status are recorded in [../STATE.md](../STATE.md).

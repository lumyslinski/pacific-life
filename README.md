# Local Snowflake-backed insurance calculation workflow

This repository is an executable local integration target for the insurance
calculation workflow. It keeps the production execution boundary:

```text
Vue frontend -> Litestar API -> formula-free Python worker
             -> Snowflake Python connector -> local Snowflake emulator
             -> database-side SQL functions -> authoritative model/audit tables
```

The worker never executes Python callbacks or formulas read from SQL. The
database catalogue maps each parameter to a versioned SQL function, argument
sources and dependencies. The local service speaks the ordinary Snowflake
connector protocol and compiles the supported `LANGUAGE SQL` scalar-function
subset to DuckDB macros.

The seeded insurance parameters are `Death`, `AccidentalDeath`,
`TotalPermanentDisability`, and `CriticalIllness`. Values are decimal strings
to avoid JavaScript floating-point rounding. The numbers are test fixtures,
not actuarial advice.

## Documentation map

Each topic is owned by exactly one document; the others link to it.

| Document | Owns |
|---|---|
| this `README.md` | Overview, how to run, audit storage, scale and safety boundaries |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Implemented version 2 requirements and lifecycle rules; revision 3 target requirements |
| [FLOW.md](FLOW.md) | Request-to-database sequence and the worked policy example |
| [STATE.md](STATE.md) | What is implemented, verification record, maintenance rule |
| [api/README.md](api/README.md) | HTTP contract: endpoints, Vue client generation, CORS/deployment, shared-type policy |
| [api/openapi.yaml](api/openapi.yaml) | Canonical versioned OpenAPI 3.1 contract |
| [api/openapi-v3-draft.yaml](api/openapi-v3-draft.yaml) | Proposed contract publication and asynchronous run API; design only, not served |
| [architecture/README.md](architecture/README.md) | Business explanation and twelve diagrams, revision 3 target (contracts, Bronze → Silver → Gold, background assessments and retries), implemented runtime and required changes |
| [examples/vue/README.md](examples/vue/README.md) | Vue 3 adapter setup |

The runtime implements **version 2**. **Revision 3** adds DataContract, the
Core phase (Bronze → Silver) and subsequent custom runs with input overrides
(Silver → Gold). This is a design/requirements update only; see `REQUIREMENTS.md` for the delta,
`architecture/README.md` for the design and `STATE.md` for the implementation
gap.

The AWS target stores published contracts in Snowflake and resolves them in
Python at command time. A run freezes its input and execution plan before the
API returns 202. A transactional run outbox feeds SQS FIFO; a separate ECS
Fargate worker claims a fenced lease, executes SQL and commits the result.
Vue polls persisted run status. Optional EventBridge completion delivery has
its own outbox. See [contract storage and AWS orchestration](architecture/README.md#contract-storage-and-publication-target)
and [page 8](architecture/08-contract-storage-aws-orchestration.svg). These
services and endpoints are proposed; the local version 2 API remains synchronous.

Start with [Core phase, then custom runs](architecture/09-parameter-layer-sequence.svg).
The coloured sequence has actors at the top with business and technical names,
including **DataContract**. Core transforms Bronze input into immutable Silver.
The user can then select any published custom process—Regional is the example—
and override parameters in that run's input.
It uses `{ DeathPenatly: 1000 }` → immutable core value 800 → user-edited
regional value 700 → completed regional value 630, with full model names.

The supporting [background execution sequence](architecture/11-assessment-client-updates.svg)
and [automatic and user-requested retry sequence](architecture/12-calculation-retry-sequence.svg)
put participants across the top and show chronological messages along
lifelines. The [one-property Data Contract](architecture/death-penatly-contract.example.yaml)
defines the example rules. [Who performs the assessment?](architecture/10-assessment-worker.svg)
provides a supplementary responsibility map.
In the proposed service, a Python worker picks up the saved assessment and
asks Snowflake to apply the selected policy formulas. The screen receives an
assessment reference immediately, then checks for **Ready** or **Failed**
while the worker continues independently. The
[business explanation](architecture/README.md#an-assessment-in-business-terms)
describes what happens at each step, including retries and returning later.
The [technology mapping](architecture/README.md#which-technology-provides-each-part)
identifies the waiting list as Amazon Simple Queue Service and the worker as
a separate Python program deployed on AWS Fargate. It also distinguishes the
existing calculation class from the queue consumer that still needs to be built.

## Lifecycle

The API has two different model lifecycles:

1. **Core Calculation** accepts policy parameters and an exact core
   configuration revision. It must bind every supplied parameter. On success
   it writes a revision-1 immutable model snapshot. There is no update or
   in-place recalculation operation for a core model.
2. **Regional Calculation**, or another named process, forks a separate
   variation from the core or from an explicitly pinned variation revision.
   The variation is mutable through optimistic revisions. Its current head can
   be edited and calculated; every change remains readable in history.

Published SQL functions and configuration revisions are also immutable. A
function draft can be edited and previewed, but a published release is always
addressed by its exact `functionId` and `functionRevision`.

The complete rule set is in [REQUIREMENTS.md](REQUIREMENTS.md); the endpoints
that implement it are listed in [api/README.md](api/README.md); a concrete
policy walk-through is in [FLOW.md](FLOW.md).

## Run locally

The Python environment needs Python 3.12+:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

Start the emulator and API as separate processes:

```bash
python -m snowflake_emulator --host 127.0.0.1 --port 8084 --database data/emulator.duckdb
EMULATOR_PORT=8084 python -m calculation_api --init-demo --host 127.0.0.1 --port 8000
```

Or use Compose (it runs `calculation_api.bootstrap` instead of `--init-demo`):

```bash
docker compose up --build
```

The browser-facing API is `http://localhost:8000`; the emulator health URL is
`http://localhost:8084/_emulator/health`. The request example is
`examples/request.json`. To run the full concrete flow and save all returned
models/audit pages to `examples/lifecycle-result.json`:

```bash
python examples/lifecycle_demo.py --base-url http://127.0.0.1:8000
```

## Audit storage

Snowflake (or this emulator) is authoritative: the model snapshot, audit event,
outbox row and command receipt commit in one SQL transaction. The
`calculation_api.audit_export` process copies committed events to PostgreSQL
(`calc."AuditEvent"`, Alembic revision `0005` in
[gea/db/postgres](gea/db/postgres/README.md)). It marks an outbox row delivered
only after the event is stored. Retries are safe: an event is one row keyed by
its id, a repeated delivery changes nothing, and different content under the
same id is an error. The table is append-only and keeps the canonical JSON
text with its SHA-256.

PostgreSQL holds a copy of the audit trail; it is not the formula catalogue or
the calculation authority. The exporter polls `AUDIT_OUTBOX JOIN AUDIT_EVENTS`
through its own Snowflake connection and inserts with psycopg; no SQS or
EventBridge is in this implemented path. The target adds separate run-dispatch and completion
outboxes without sharing this exporter's delivery flag. Production run
ownership needs enforced uniqueness, fenced leases and conditional result
publication, as described in [AWS orchestration](architecture/README.md#aws-run-orchestration-target-page-8).
This local API serializes writers in one process.

For Compose, start the optional profile; it also starts PostgreSQL and runs
the migrations:

```bash
docker compose --profile audit up --build
```

Outside Compose, the login only needs to be a member of role `calc_audit`:

```bash
AUDIT_DATABASE_URL=postgresql://calc_audit_exporter:secret@localhost:5432/gea \
python -m calculation_api.audit_export          # --once for a single pass
```

## Scale and safety boundaries

The worker batches invocations by function and dependency wave, correlates
results with `CALL_ID`, and performs objective aggregation in SQL. The local
default is 256 function calls per batch and 100 policies/parameters per API
request; `CALC_MAX_POLICIES` and `CALC_MAX_PARAMETERS` can be changed, with
`0` disabling a guard. The emulator serializes SQL execution in one process,
so representative load tests are required before selecting production batch
sizes.

Supported local SQL is scalar `LANGUAGE SQL` expressions, table DDL/DML and
the connector features used by this workflow. Python/JavaScript UDF handlers,
stored procedures, table functions, asynchronous queries and external access
are rejected intentionally. Editable versioned functions cannot read mutable
lookup tables; pin lookup data as constants or publish a versioned data
revision. This is a supported SQL/connector subset, not complete Snowflake
emulation or a production distributed scheduler.

## Tests

For connector/API tests without PostgreSQL:

```bash
SNOWFLAKE_DISABLE_PLATFORM_DETECTION=true \
AWS_EC2_METADATA_DISABLED=true \
python -m pytest -q tests/test_connector.py tests/test_calculation_api.py
```

The audit export tests and the GEA API tests need a PostgreSQL 16 database at
the Alembic head. Without the two variables they are skipped:

```bash
export GEA_DATABASE_URL=postgresql://postgres@localhost:5432/gea_test
alembic -c gea/db/postgres/alembic.ini upgrade head
AUDIT_TEST_DATABASE_URL=$GEA_DATABASE_URL GEA_TEST_DATABASE_URL=$GEA_DATABASE_URL python -m pytest -q
```

The latest recorded results are in [STATE.md](STATE.md).

## GEA module

Projects, the run wizard and run data contracts for PL Re GEA live next to the calculation service:

| Where | What |
|---|---|
| [`gea/`](gea/README.md) | Decisions, the field list of the workbook, the OpenAPI contract, the Vue client |
| [`gea/EXECUTION.md`](gea/EXECUTION.md) | How a run executes: Snowflake computes, PostgreSQL decides when a run starts, Python only relays |
| [`gea/db/postgres/`](gea/db/postgres/README.md) | PostgreSQL schema and its Alembic migrations |
| [`gea_api/`](gea/api/README.md) | Litestar API on PostgreSQL (`python -m gea_api`, port 8010) |
| [`tests/gea/`](tests/gea) | Unit tests and API tests against PostgreSQL |
| [`architecture/`](architecture/README.md) | Diagrams, pages 13 to 17 |

```bash
docker compose --profile gea up          # postgres, alembic upgrade head, then the API on http://localhost:8010/gea/v1
```

It is a separate process with its own store; nothing in `calculation_api` or `snowflake_emulator` depends on it.

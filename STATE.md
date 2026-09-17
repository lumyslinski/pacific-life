# Implementation state — 2026-09-17

This file records **what is implemented, what is verified, and what is still
open**. Run instructions and limits live in [README.md](README.md);
requirements in [REQUIREMENTS.md](REQUIREMENTS.md); the HTTP contract in
[api/README.md](api/README.md); the target design, runtime diagrams and
required code changes in [architecture/README.md](architecture/README.md).

## Implemented (version 2)

- Local connector-compatible Snowflake HTTP emulator backed by DuckDB.
- Persistent logical databases/schemas, scalar SQL UDF metadata and supported
  Snowflake SQL compilation.
- Editable typed SQL drafts, preview, immutable releases and release pinning.
- Immutable configuration revisions containing per-parameter function
  bindings, argument mappings, constants, dependencies and objective release.
- Core Calculation with transactionally saved immutable model snapshots.
- Regional Calculation/named-process variations with branching, chaining from a
  pinned revision, optimistic edits and immutable revision history.
- Database-side batched function execution with `CALL_ID` correlation and SQL
  objective aggregation; formulas are not duplicated in Python.
- Request replay by stable `requestId`; stale variation/config/function edits
  return HTTP 409.
- Transactionally coupled model/audit/outbox/command records.
- Optional boto3 DynamoDB audit projection with idempotent writes, retry-safe
  outbox handling and large-event chunk/manifest integrity checks.
- Official DynamoDB Local 3.3.1 test launcher in
  `tests/run_with_dynamodb_local.py`; Docker Compose `aws-audit` profile.
- Canonical OpenAPI 3.1 contract in `api/openapi.yaml` (17 paths, 38 schemas).
- Vue 3 + TypeScript client/composable example in `examples/vue` using
  `openapi-fetch`. Type generation with `openapi-typescript` is documented;
  `generated.ts` and a runnable Vue application are not included yet.

## Architecture documentation

- Single architecture reference: `architecture/README.md` (revision 3 target
  design, implemented runtime, required changes, source traceability) and
  `architecture/data-contract.example.yaml`. The separate
  `architecture/death-penatly-contract.example.yaml` defines the one-property
  business example; it is proposed and is not seeded in the running system.
- One editable draw.io file, `architecture/calculation-architecture.drawio`,
  with twelve pages and matching SVG previews: pages 1–3 are the target layers
  (layer overview, Data Contract, Silver to Gold); pages 4–7 are the
  implemented runtime (components, functions/bindings, calculation sequence,
  audit sequence); page 8 is proposed contract storage and AWS orchestration.
  Pages 3 and 9 use the DeathPenatly example with full names: original 1000,
  immutable core 800, user-edited regional input 700 and completed Gold 630.
  A later regional edit to 600 produces a new result of 540.
  Page 9 is now the main sequence: five actors at the top with business and
  technical names, including DataContract; Core transforms Bronze to Silver
  before the user selects a custom process and overrides its input. Regional
  is an example custom process, not a mandatory second phase.
  Pages 10–12 name the actual technologies as well as their business roles:
  Litestar application, Amazon Simple Queue Service, Python worker on AWS
  Fargate, and Snowflake formula execution and storage. Page 11 is a supporting
  actor/lifeline sequence including published Data Contract resolution,
  asynchronous execution and parallel client status checks. Page 12 shows
  bounded automatic retries and the eligible Retry calculation action with
  the same frozen input/contract/plan. Pages 3 and 9–12 have PNG previews.
  `architecture/generate.py` regenerates both formats from the same scenes.
- Diagram checks (2026-09-17): twelve-page draw.io XML and all twelve SVG files
  parse; generated previews match their scenes and draw.io labels; all 364
  editable text labels have explicit white backgrounds and dark text. Page
  geometry and Arial text fit checks passed. All eight SVGs were rendered to
  PNG and visually reviewed together, with page 8 also inspected at larger
  size. The rebuilt pages 3 and 9–11 were rendered and visually reviewed;
  pages 11–12 were subsequently rebuilt as full sequences, rendered and
  visually reviewed. The DeathPenatly example arithmetic was verified with SQL.
  The corrected Core/custom sequence and updated headers were subsequently
  regenerated and visually reviewed.
  Runtime pages remain explicitly version 2; their subtitles distinguish
  them from the proposed AWS deployment. Lucidchart import is still untested.
- Both contract YAML examples parse and dependency/argument-source checks pass.
  The one-property contract's cap and factor produce 800 / 630 / 540 in SQL.
  Its illustrative regional adjustment function still needs publication.
  Publication status is now external to the immutable document. The unchanged
  Silver and first/second Gold fixture values were previously verified with DuckDB SQL on
  2026-09-16.
- `api/openapi-v3-draft.yaml` is a design-only partial specification for
  contract publication and asynchronous run resources (6 paths, 13 schemas).
  Its phase values are now `core` and `custom`; custom commands require the
  exact published `processName`. Both example contracts use the same phase names.
  The served `api/openapi.yaml` still describes version 2; Vue examples use it.

## Revision 3 — design only, not implemented

The revision 3 target (Data Contract module, retained Bronze input, immutable
Silver Core output, editable custom scenario overrides, versioned Gold
results, Snowflake contract publication, SQS/Fargate run dispatch and fenced
leases) is specified in [REQUIREMENTS.md](REQUIREMENTS.md) and designed in
[architecture/README.md](architecture/README.md). None of the design or
diagram work executed or changed the application workflow.

Implementation gap against the current code:

- `ModelLifecycle.change(calculate=True)` still uses the variation's current
  results, so percentage transformations can compound (lifecycle rule 3 in
  `REQUIREMENTS.md`). Revision 3 must rebuild from pinned Silver/source plus
  scenario edits.
- Scenario drafts are not yet separated from Gold output.
- Durable failed-run/attempt records and frozen-plan retry do not exist yet.
- Add `CONTRACT_DRAFTS`, immutable `CONTRACT_REVISIONS`, separate contract
  lifecycle status, publication validation, canonical hashes and the optional
  cache keyed by exact revision/hash. `CONFIG_REVISIONS.DOCUMENT_JSON` is the
  existing pattern; it is not yet a Data Contract repository.
- Add `RUNS`, `RUN_ATTEMPTS`, `RUN_OUTBOX`, unique request receipts, immutable
  manifests and atomic acceptance. Split API, run relay and Fargate worker;
  implement SQS FIFO dispatch, claims, fenced leases, heartbeat/visibility
  extension, bounded retries, expired-lease recovery and DLQ reconciliation.
- Prove production uniqueness, ownership and result-commit transactions
  across multiple sessions. Hybrid control tables are the preferred candidate
  subject to supported payload types/deployment constraints. FIFO and the
  existing process-local `RLock` do not establish distributed ownership.
- Implement `POST /runs` → 202, `GET /runs/{runId}`, explicit retry and contract
  endpoints from the draft OpenAPI. Promote the schema alongside handlers and
  integration tests, regenerate TypeScript and adapt Vue to status polling.
- Retain the direct `AUDIT_OUTBOX → audit-export → DynamoDB` path. Add an
  independent `RUN_EVENT_OUTBOX` and optional EventBridge completion relay;
  no queue or event-bus support currently exists in `audit_export.py`.
- Explicit parent chaining remains supported but must become a deliberate
  choice rather than the default rerun path.
- Step Functions Standard or Temporal remains a Stage 2 option for durable
  human waits and regional fan-out, not a current runtime dependency.

## Verification record

Last full run: **2026-09-15** (the 2026-09-16 and 2026-09-17 checkpoints were documentation
and diagram changes only and did not rerun the suite).

| Check | Result |
|---|---|
| Python compilation (`python -m py_compile`) for emulator, API, tests and demo scripts | passed |
| Served OpenAPI YAML parse, local references, path parameters and unique operation IDs | passed — 17 paths, 38 schemas (2026-09-17) |
| Proposed OpenAPI YAML parse, local references, path parameters and unique operation IDs | passed — 6 paths, 13 schemas (2026-09-17); not a runtime endpoint test |
| Diagram XML, scene/output parity, label styling, bounds and text fit | passed — 12 pages, 364 labels (2026-09-17); rendered previews reviewed |
| One-property business example arithmetic | passed — core cap yields 800; regional edits 700 / 600 yield 630 / 540 |
| Local Markdown file links and contract YAML dependency checks | passed (2026-09-17) |
| Full suite with official DynamoDB Local 3.3.1 (`python tests/run_with_dynamodb_local.py /path/to/DynamoDBLocal -q`) | **28 passed in 104.38s** |

The full suite covers connector behavior, API lifecycle (immutable core,
independent/chained variations, custom SQL releases, stale edits, rollback,
batching and frozen snapshots), and audit outbox/projection recovery. How to
run the tests is documented in [README.md — Tests](README.md#tests).

## Change log

| Date | Checkpoint |
|---|---|
| 2026-09-15 | Full suite run with DynamoDB Local (28 passed); OpenAPI 3.1 contract, Vue client example and `examples/lifecycle_demo.py` delivered |
| 2026-09-16 | Runtime diagrams created from the source; revision 3 design (Data Contract, Bronze → Silver → Gold, Gold delivery loop) and contract YAML added; documentation only |
| 2026-09-16 | Documentation consolidated: one owner per topic (`README.md` documentation map), `architecture/` reduced to one README, one generator and one seven-page draw.io file; `ARCHITECTURE_V3.md`, `diagrams/`, `WORK_STATE.md` and `test-results.txt` folded in and removed |
| 2026-09-17 | Reviewed storage/orchestration findings; documented contract publication in Snowflake and Stage 1 SQS/Fargate runs, fencing/recovery and separate completion outbox; updated all READMEs, requirements and flow; added draft run/contract OpenAPI and page 8; regenerated all diagrams; documentation only |
| 2026-09-17 | Added page 9: coloured Bronze/Silver/Gold sequence, one-parameter dummy model, immutable core output, editable regional drafts and preserved Gold history; SVG/draw.io plus PNG preview; documentation only |
| 2026-09-17 | Added business diagrams 10–11 and plain-language explanation: what a worker does, Snowflake formula execution, queued assessment acceptance, automatic client status checks, ready/failed outcomes and recovery; updated page 9 cross-references; documentation only |
| 2026-09-17 | Rebuilt business diagrams with full model/request names and the exact DeathPenatly property; replaced unexplained shorthand with business roles plus concrete AWS/Python/Snowflake technologies; documented that the calculation class exists but its queue consumer and cloud deployment are proposed |
| 2026-09-17 | Replaced page 11 with an eight-participant sequence including the Data Contract; added page 12 for automatic and user-requested retry, preserved frozen input/rules, and added the one-property contract YAML; updated documentation links and requirements; documentation only |
| 2026-09-17 | Corrected the main sequence to start with Core transforming Bronze to Silver, then user-selected custom processes with input overrides; put business/technical actor names and DataContract in the header; generalized the draft API and contract examples to core/custom plus processName; runtime unchanged |

## Maintenance rule

When an endpoint or payload changes, update `api/openapi.yaml`, regenerate the
Vue types, add/adjust an integration test, then update this file in the same
change. When the runtime or the design changes, edit the scene definitions in
`architecture/generate.py`, rerun it, and update `architecture/README.md`.

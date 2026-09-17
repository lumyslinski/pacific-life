# Requirements — implemented version 2 and revision 3 target

The runtime implements **version 2** (`GET /health` reports
`lifecycleVersion: 2`). **Revision 3** adds a Data Contract module and the
Bronze → Silver → Gold calculation lifecycle and is the target for the next
implementation. In both, the Python framework is **Litestar** and formulas
execute as database-side SQL functions. Implementation status against these
requirements is tracked in [STATE.md](STATE.md); the HTTP contract is
[api/openapi.yaml](api/openapi.yaml), summarized in
[api/README.md](api/README.md).

## Revision 3 target — Data Contract and data layers

| Requirement | Required behavior | Implementation change |
|---|---|---|
| DataContract module | Publish immutable specifications for parameter schema, Core/custom phase bindings, named custom processes, arguments, dependencies, objective and permitted input overrides; Python resolves published Snowflake documents at command acceptance | Extend the current configuration catalogue and validation behind a dedicated module |
| Contract storage/publication | Optimistic mutable drafts; immutable ID/revision/hash/document and publisher metadata; validate every exact function release/signature and dependency graph before publication | Add `CONTRACT_DRAFTS`, `CONTRACT_REVISIONS`, separate deprecation status and publish API; enforce revision uniqueness |
| Contract pinning/cache | Resolve latest only once after receipt lookup; freeze the exact contract/input/plan and hashes; retry never reads a newer draft | Run manifest and optional process-local cache keyed by ID/revision/hash; Snowflake remains authoritative |
| Core phase: Bronze → Silver | First retain the Bronze input, then validate/map and calculate every supplied parameter under its DataContract; publish Silver only after complete success | Bronze is an input data layer, not a separate calculation phase |
| Silver layer | Publish only complete successful Core Calculation output; keep it immutable | Existing core snapshots become the Silver resource, with Bronze lineage |
| Editable custom scenarios | After Core succeeds, allow the user to choose a named custom process and override permitted input parameters in a separate scenario | Persist process name, source reference, input overlay and draft revisions separately from calculated output |
| Custom Calculation | Regional Calculation is one possible process, not a fixed second phase; assemble pinned Silver plus user overrides, apply the selected process rules and retain every other value | Generic custom phase with a DataContract-defined process name; rebuild from source and overlay for each new run |
| Gold layer | Each successful custom run delivers a complete versioned Gold model; the user then decides to process it further or finish | Append result snapshots linked to run, process, scenario revision, contract and source |
| Retry semantics | Duplicate submits return the same run; failed-run retry uses the same frozen input/plan; successful-run retry returns its saved result | Durable run manifest, attempts and idempotent retry command |
| Retry experience | Show Retrying during bounded automatic recovery; show Retry calculation only for eligible failed work; resume status checks for the same run and retain its original Data Contract | Persisted retry eligibility and attempt allowance; repeated commands do not create competing work; [actor sequence](architecture/12-calculation-retry-sequence.svg) |
| Changes after a run | New settings or function releases require a new scenario revision/run | Earlier runs keep their original contract and function releases |
| Failed calculation | Publish no partial Silver or Gold output; retain the source and attempt diagnostics | Persist run/attempt state outside the rolled-back result transaction |
| Explicit sequential regions | After a Gold delivery the user may continue the pipeline: chaining pins that Gold result as the source of a new scenario; keep original Silver lineage | Preserve explicit chaining while default reruns always use Silver |
| Concurrent results | A run publishes only for its pinned draft and cannot move a newer scenario's result pointer backwards | Conditional revision/run ownership checks |
| Asynchronous run API | Accept core/custom commands with 202 and run ID; custom commands select a published processName; persist authoritative status/result reference and support frozen-plan retry | `POST /runs`, `GET /runs/{runId}`, `POST /runs/{runId}/retry`; Vue polling with ETag, optional later SSE |
| Durable AWS dispatch | Commit run, request receipt and dispatch outbox together; relay sends to SQS FIFO before marking delivery | `RUNS`, `RUN_ATTEMPTS`, `RUN_OUTBOX`; separate ECS Fargate API, relay and worker |
| Distributed ownership | Enforce unique command/run identities, conditional claims, lease heartbeats and fencing at result commit; FIFO alone is insufficient | Validate production control-table concurrency and cross-table transaction support; retain scenario pointer guards |
| Queue recovery | Deduplicate persistently beyond the FIFO window; explicit retry gets a new dispatch ID/generation; bounded retries survive re-enqueue | Lease reaper, persisted attempt budget, SQS visibility extension/backoff and DLQ reconciliation; terminal failure remains visible through API |
| Completion delivery | Result, lineage, success audit/outboxes and succeeded status commit atomically; no direct post-commit event dual write | Independent `RUN_EVENT_OUTBOX` for optional EventBridge completion relay; retain direct DynamoDB audit exporter and its own delivery flag |
| Multi-step orchestration | Introduce one durable workflow engine when human waits/fan-out/chaining need shared workflow state | Stage 2 option: Step Functions Standard or Temporal above the same idempotent run primitives |

The mutable user object is the custom scenario/head. Silver and historical
Gold snapshots stay immutable. This preserves the earlier requirement for
editable model variations without rewriting past calculation results.

The full target design and concrete function mappings are in
[architecture/README.md](architecture/README.md) and
[architecture/data-contract.example.yaml](architecture/data-contract.example.yaml).
The new behavior is not implemented. [api/openapi-v3-draft.yaml](api/openapi-v3-draft.yaml)
records the proposed contract/run endpoints; `api/openapi.yaml` continues to
describe the served version 2 API.

## Implemented version 2 baseline

| Requirement | Required behavior | Implemented contract |
|---|---|---|
| Editable functions | Edit typed SQL arguments, return type and SQL expression; preview with supplied values | `PUT /functions/{id}`, then `POST /preview` |
| Reproducible functions | Publishing creates a new immutable SQL function release; edits never change old releases | `POST /publish`; exact `functionId` + `functionRevision` references |
| Configurable models | Persist parameter mappings, constants, dependencies and objective function | Immutable `POST /configs/{id}/revisions` |
| Crucial core | Core Calculation must have a binding for every supplied parameter; complete successfully or save no model | `POST /core-models` within one SQL transaction |
| Immutable core output | Core output, inputs, settings, function releases and hash remain fixed | Insert-only `CORE_MODELS`; no edit or in-place recalculation API |
| User-triggered second process | Regional Calculation or another named process creates a separate model variation | `POST /variations`, then `POST /variations/{id}/calculate` |
| Mutable variations | User may edit values and settings; every edit/calculation creates a retained revision | `PATCH /variations/{id}` with `expectedRevision` |
| Reuse and customization | Inherit functions, select a regional configuration, or override a binding per parameter | `bindingOverrides` can change release, mappings and constants, or remove a binding |
| Branch and chain | Independent regions use the same immutable core; another process may fork a specific variation revision | `parentVariationId` + `parentRevision`; preserve original `coreModelId` |
| Core functions in later processes | A variation can use the same SQL functions as core without mutating core | Omit `configId` when forking directly from core; assign another process name |
| Audit every committed operation | Save before/after results, SQL function identities, arguments, objective and query IDs | Model, audit event, command receipt and outbox commit together |
| Optional AWS audit database | Export audit through the AWS SDK to a local AWS database emulator | DynamoDB Local, optional Compose profile `aws-audit` |
| Safe retries | Repeating the same command returns the same response; stale concurrent edits fail | `requestId` replay and optimistic `expectedRevision`, HTTP 409 |
| Scale within tests | Batch function calls across policies instead of one network call per parameter | Dependency waves; 256 calls per transformation batch; objective aggregation in SQL |

## Implemented version 2 lifecycle rules

1. A core model has revision 1 permanently. A new core run creates a different model ID.
2. A variation has a mutable current head and immutable historical snapshots. Creation is revision 1 (`draft`); calculation is revision 2 (`calculated`); a later edit becomes revision 3 (`draft`).
3. **Legacy behavior to change in revision 3:** calculation consumes the variation's current values, so a non-idempotent formula compounds on another new calculation. Version 3 must rebuild from pinned Silver/source plus scenario edits. Until implemented, replay the same `requestId` or fork the original source for a fresh scenario.
4. A child variation pins its parent's revision and content hash. Later parent edits do not change that child. A parent must belong to the same core.
5. In Core Calculation every input parameter requires a binding. In a variation, parameters without a selected binding retain their values. Use an explicit identity SQL function when a core parameter should pass through.
6. Changing a regional label alone does not silently select a different function configuration. Supply its `configId` and `configRevision` explicitly.
7. Configurations, published functions and historical model snapshots never use a floating `latest` reference. A variant stores its complete effective settings after overrides.
8. Core objective baseline is its input. Variation objective baseline is the original immutable core output, including when a variation is chained from another region.
9. SQL function releases may call other explicitly versioned releases. The editable catalogue excludes nondeterministic builtins and reads from mutable lookup tables; supply lookup values as pinned constants.

## Acceptance and scope

Revision 3 must additionally verify the new Data Contract rules, Bronze
retention after failure, fresh Silver-based reruns, frozen-plan retries,
scenario/result separation, complete Gold output, and concurrency around the
scenario's latest successful result. See
[architecture/README.md](architecture/README.md) for the non-compounding
0.90/0.80 example and the proposed retry behavior.

AWS acceptance must also cover contract-publication races, command replay
after a newer contract is published, relay crashes after send, duplicate
deliveries outside FIFO's deduplication window, manual retry inside that
window, lease expiry and late workers, commit-before-ack crashes, persisted
retry budgets, DLQ reconciliation and independent audit/completion delivery.
Production concurrent-session tests must prove the chosen control-table
ownership and atomic result protocol; the local request lock cannot do so.

The integration suite must verify frontend HTTP calls through the real
Snowflake connector to the local service, immutable core behavior, independent
and chained regions, custom function publication, revision conflicts,
transaction rollback, batching, and audit export recovery.

This implementation targets local integration tests and simulations.
Parameters are nonnegative `NUMBER(18,2)` amounts; constants may use other
supported scalar types. There is no hardcoded parameter list in the worker,
but the API applies configurable count limits. The supported SQL subset,
batch sizes and concurrency limits are documented once in
[README.md — Scale and safety boundaries](README.md#scale-and-safety-boundaries).

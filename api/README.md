# API contract and Vue integration

`openapi.yaml` is the language-neutral contract for the implemented Litestar API.
It is the source for the current Vue client. Python implementation classes
are not imported or copied into the browser bundle. The running API also
serves the same document at `GET /openapi.yaml`.

The document describes the implemented **version 2** API (`GET /health`
reports `lifecycleVersion: 2`). The **revision 3** Data Contract and
Bronze → Silver → Gold behavior specified in
[../REQUIREMENTS.md](../REQUIREMENTS.md) and
[../architecture/README.md](../architecture/README.md) has not been added to
this served schema yet. [openapi-v3-draft.yaml](openapi-v3-draft.yaml) describes
the proposed contract/run endpoints for design review; it is not served by the
API and is not a complete replacement specification for all model resources.

## Endpoints

The contract covers function drafts/releases, configuration revisions, Core
Calculation, variation branching/editing/calculation, audit pagination,
idempotent `requestId` and optimistic `expectedRevision` handling. The
lifecycle rules behind these endpoints are in
[../REQUIREMENTS.md](../REQUIREMENTS.md).

```text
GET   /health                                    liveness and lifecycle version
GET   /functions                                 list SQL function drafts
GET   /functions/{functionId}                    read current draft
PUT   /functions/{functionId}                    edit SQL draft (expectedRevision)
POST  /functions/{functionId}/preview            preview draft in the DB
POST  /functions/{functionId}/publish            create immutable release
GET   /functions/{functionId}/releases/{version} read a published release
GET   /configs                                   list configuration revisions
POST  /configs/{configId}/revisions              publish immutable bindings
GET   /configs/{configId}/revisions/{version}    read a configuration revision
POST  /core-models                               create immutable core
GET   /core-models/{modelId}                     read core snapshot
POST  /variations                                fork mutable variation
GET   /variations/{modelId}                      read current variation head
PATCH /variations/{modelId}                      create next edit revision
POST  /variations/{modelId}/calculate            create next calculated revision
GET   /variations/{modelId}/revisions/{version}  read immutable history
GET   /audit/{aggregateId}                       read committed audit events (paged)
POST  /simulations                               legacy; always HTTP 410
```

The old `POST /simulations` endpoint intentionally returns HTTP 410. It would
blur the immutable-core/mutable-variation boundary.

## Why the boundary is OpenAPI

- Python owns validation, lifecycle rules, SQL-function resolution, optimistic
  concurrency and persistence.
- Vue owns form state, navigation, display and user retries.
- TypeScript types are generated from the same OpenAPI document, so a changed
  request or response is detected during the frontend build.
- Insurance parameter names remain dynamic (`Death`, `AccidentalDeath`,
  `TotalPermanentDisability`, `CriticalIllness` are seeded examples), rather
  than hard-coding a Python or TypeScript class per product.
- Formulas, Snowflake credentials and database table names never enter Vue.

## Proposed revision 3 contract and run endpoints

For the business flow behind these endpoints, see
[Core phase, then custom runs](../architecture/09-parameter-layer-sequence.svg),
[background execution with DataContract resolution](../architecture/11-assessment-client-updates.svg),
[the automatic and manual retry sequence](../architecture/12-calculation-retry-sequence.svg)
and the [client update explanation](../architecture/README.md#how-the-client-learns-that-work-finished-or-failed).
The initial response acknowledges a saved request. Later status checks tell
the screen whether the assessment is waiting, running, ready or failed; they
do not start another calculation.

These endpoints are **not implemented**. The domain Data Contract describes
calculation schema/bindings; OpenAPI describes its HTTP transport. Published
domain documents live in Snowflake and only Python resolves them into plans.

| Endpoint | Target behavior |
|---|---|
| `GET /contracts/{contractId}` | Read the current mutable draft |
| `PUT /contracts/{contractId}` | Save a draft using `expectedRevision` (0 for creation) |
| `POST /contracts/{contractId}/publish` | Validate the exact draft and function releases, then create an immutable published revision |
| `GET /contracts/{contractId}/revisions/{revision}` | Read an exact published document and hash |
| `POST /runs` | Freeze a Core or custom command; custom includes processName; commit run/receipt/dispatch outbox and return 202 with runId, pinned contract and Location |
| `GET /runs/{runId}` | Return persisted status, attempts and result reference; support ETag and 304 |
| `POST /runs/{runId}/retry` | Idempotently retry failed work using the same frozen plan; 202 for active/queued work, 200 for an existing success |

Example custom command selecting Regional Calculation (design only):

```json
{
  "requestId": "regional-command-42",
  "phase": "custom",
  "processName": "Regional Calculation",
  "scenarioId": "scenario-7",
  "expectedRevision": 2,
  "contract": {"contractId": "INSURANCE_CALCULATION", "revision": 3}
}
```

Core is the first phase: its Bronze input becomes immutable Silver on success.
The user can then choose a custom process defined by a published DataContract;
Regional Calculation is one example. The scenario supplies the process name,
permitted input overrides, and its pinned Silver or explicitly chained Gold source;
the command cannot silently replace that source or select a different process.
Unchanged parameters retain their source values; overrides replace selected
values in the assembled input, without editing Silver. Core commands reference a
retained Bronze snapshot. The API verifies the requested contract against the
scenario, freezes input/settings and returns the accepted run. A
`latestPublished` selector resolves once, after receipt lookup; replay cannot
select a newly published revision. Reuse of a request ID with a different
payload or a stale scenario/draft revision returns 409.

Status progresses `QUEUED → RUNNING → SUCCEEDED`, with `RETRYABLE` for pending
automatic retries and `FAILED` for terminal failure. Vue follows `Location`,
polls using `If-None-Match`, then fetches the immutable result reference after
success. New inputs require a new draft/run; retry never accepts a replacement
contract or payload. Persisted run state remains authoritative if notification
delivery is delayed. SSE and full Bronze/scenario/Silver/Gold resource routes
will be specified when implemented.

For `FAILED` work, show **Retry calculation** only when `retryEligible` is
true. That action sends a new idempotent retry command for the same run and
resumes status checks on the same reference. Keep its request ID for repeated
clicks or transport retries. The backend grants a bounded attempt allowance
and creates a new dispatch generation; it does not re-read the latest contract
or substitute an edited scenario. Active work returns its existing state;
successful work returns its saved result.

The [AWS design](../architecture/README.md#aws-run-orchestration-target-page-8)
defines outboxes, SQS dispatch, fenced worker leases, attempt budgets and
completion delivery. Existing `POST /core-models` and variation calculation
responses remain synchronous in version 2. Promote the draft and adapt the
Vue flow only alongside implementation and integration tests.

## Generate a Vue client

From the Vue application directory (`examples/vue/` in this repository; adjust
the relative path to `api/openapi.yaml` for another layout):

```bash
npm install openapi-fetch
npm install --save-dev openapi-typescript
npx openapi-typescript ../../api/openapi.yaml -o src/api/generated.ts
```

`examples/vue/src/api/client.ts` uses the generated `paths` type with
`openapi-fetch`. Commit the generated file or regenerate it in CI and fail the
build when the generated diff is non-empty. `generated.ts` is not checked in;
see [../examples/vue/README.md](../examples/vue/README.md) for the adapter
setup.

## Environment and deployment

Use `VITE_API_BASE_URL` for the API origin. For production, prefer a same-
origin reverse proxy (`/api`) so cookies, CORS and browser security policies
are controlled at one boundary. If the Vue app is hosted separately, configure
the Litestar `FRONTEND_ORIGINS` allow-list (comma-separated exact origins;
default `http://localhost:4200,http://localhost:5173`, Compose adds
`http://localhost:3000`); never use `*` for credentialed requests.

The browser calls only Litestar. Litestar calls the Snowflake connector. The
browser must never connect to Snowflake, the local emulator, DuckDB or
PostgreSQL directly.

## Sharing classes and enums

Do not share Python dataclasses with Vue. Share these artifacts instead:

1. OpenAPI schemas for transport DTOs and generated TypeScript types.
2. JSON Schema or OpenAPI enums for stable values such as `kind`, lifecycle
   status and audit event type.
3. A small generated constant module only for display labels; the backend
   remains authoritative for allowed values and function/config revisions.

The frontend should treat returned model snapshots as read-only data. Keep
`core` snapshots immutable in the UI, use `expectedRevision` for variation
edits, preserve the same `requestId` across a transport retry, and handle HTTP
409 by reloading the current variation head.

## Typical Vue flow

```text
createCore() -> createVariation(core.modelId) -> calculate(variation)
            -> editVariation(expectedRevision) -> calculate(newRevision)
            -> getAudit(modelId)
```

Custom SQL functions follow a separate admin flow: edit draft, preview,
publish an immutable release, then reference its exact `functionId` and
`functionRevision` in a variation binding override.

## Maintenance

When an endpoint or payload changes, update `openapi.yaml`, regenerate the Vue
types, add or adjust an integration test, then record the change in
[../STATE.md](../STATE.md).

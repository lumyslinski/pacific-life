# GEA API: routing, results and the wizard

[`openapi.yaml`](openapi.yaml) is the contract: OpenAPI 3.1, 24 paths, 31 operations, base path `/gea/v1`.
It is generated; edit [`openapi.base.yaml`](openapi.base.yaml) for routes and shared schemas, or
`../spec/poc-data.json` for the run fields, then run `python gea/tools/generate.py`.

The routes are named after what the business calls things (projects, runs, jobs, treaties, run parameters,
submit, resolution), starting from workbook sheet "04. API Data". The run fields are those of sheet "POC Data",
under the workbook's prop names. The API never computes: Snowflake executes a submitted run
([../EXECUTION.md](../EXECUTION.md)); the API shows where the execution is and takes the decisions about it.

The handlers are in [`../../gea_api`](../../gea_api) (Litestar). `tools/check_openapi.py` holds every operation
to the conventions below, and `tests/gea/test_api.py` checks that the registered routes are exactly the
operations of the contract.

## One answer shape

Every response body, of every operation, is the same envelope. Exactly one of its two members is null:

```json
{ "data": <the resource>, "error": null }
{ "data": null, "error": { "code": "...", "message": "...", "status": 422, "issues": [], "requestId": "..." } }
```

| | Status | `data` | `error` |
|---|---|---|---|
| Success | 200, 201, 202 | The resource. A list is named after what it lists, as in sheet 04: `{ "projects": [...], "nextCursor": null }`, `{ "runs": [...], "nextCursor": null }`, `{ "jobs": [...], "nextCursor": null }`, `{ "regions": [...] }`, `{ "datasets": [...] }`. `DELETE` answers 200 with `null`. | `null` |
| Failure | 4xx, 5xx | `null` | The error object |
| Not modified | 304 | No body at all (HTTP does not allow one) | |

The HTTP status is still the real one (a failure is never a 200), and the content type is always
`application/json`. A validation failure:

```json
{
  "data": null,
  "error": {
    "code": "validation_failed",
    "message": "2 fields are not valid.",
    "status": 422,
    "issues": [
      { "code": "unknown_field", "message": "colour is not a prop of a run.", "field": "colour", "pointer": "/colour" },
      { "code": "invalid_type", "message": "Study Period must be an object with start and end.", "field": "studyPeriod", "stepKey": "dataAndSetUp", "pointer": "/studyPeriod" }
    ],
    "requestId": "0f8f2c1e9a3b4d5c8e7f6a5b4c3d2e1f"
  }
}
```

- `code` is the stable, machine-readable reason; branch on it. `message` is the sentence for the user.
- `status` repeats the HTTP status, so the object is complete when it is passed around without the response.
- `issues` has one entry per field, all of them at once, each with a JSON Pointer into the request body; it
  is an empty list when the failure is not about fields. The same `Issue` object is used in a run
  (what is still missing) and in the review.
- `requestId` equals the `X-Request-Id` response header and identifies the request in the server log.
  A client may send its own `X-Request-Id`.
- An unexpected exception becomes `internal_error` with a generic `message`; the exception text is logged
  with the request id and never sent. Litestar's own failures (unknown route, wrong method, a path parameter
  that is not a UUID) are answered in the same envelope.

In `openapi.base.yaml` a 2xx response declares only the schema of `data`; `generate.py` wraps it in the
envelope, and `check_openapi.py` refuses an operation that answers anything else (or a 204).

The Vue client unwraps the envelope into one object, `ApiResult<T>`; see [Vue client](#vue-client).

| `code` | Status | When |
|---|---|---|
| `bad_request` | 400 | Malformed JSON, body is not an object, `Idempotency-Key` missing |
| `unauthorized` | 401 | Missing or invalid token |
| `not_found` | 404 | Unknown resource or route |
| `locked` | 409 | The run has been submitted, or the project is signed off |
| `invalid_state` | 409 | Not possible now: resolve a run that has not failed, cancel one that is not queued or running, retry a delivery that has not failed |
| `precondition_failed` | 412 | `If-Match` is stale: the resource changed after it was read |
| `precondition_required` | 428 | `If-Match` is missing |
| `validation_failed` | 422 | Unknown prop, wrong type, unknown region / business purpose / benefit, bad query parameter |
| `idempotency_key_reused` | 422 | The key was already used for a different request |
| `not_ready` | 422 | Submit while required props are empty; `issues` lists every missing prop with its step. For a job: every run that is not a complete draft, with a pointer into `runIds` |
| `service_unavailable` | 503 | Deadlock, lock or statement timeout, database unreachable; `Retry-After` is set |
| `internal_error` | 500 | Anything unexpected |

## Routing conventions

| Convention | Rule |
|---|---|
| Resources | Plural nouns the business uses, at the top level: `/projects`, `/runs`, `/jobs`, `/treaties`, `/datasets`, `/regions`. A child that cannot exist alone is nested once: `/projects/{projectId}/run-parameters`, `/runs/{runId}/contracts`. Path parameters are ids, never names. The project and the job of a run are members (`projectId`, `jobId`) and filters of the list (`GET /runs?projectId=`, `?jobId=`). |
| Methods | `GET` reads. `POST` on a collection creates (201 + `Location`). `PATCH` is a JSON Merge Patch ([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396), `application/merge-patch+json`): absent stays, `null` empties. `DELETE` answers 200 with `data: null`. There is no `PUT`. |
| Actions | What the user does to a run is a `POST` on that word below the run: `/runs/{runId}/submit`, `/cancel`, `/resolution`. Cloning creates a run: `POST /runs` with `cloneSourceId`. Submitting several runs creates a job: `POST /jobs`. |
| Operations | What only the people who run the platform need is under `/operations`, apart from the business routes: `POST /operations/deliveries/{contractId}/retry`. |
| Idempotency | A `POST` that creates requires an `Idempotency-Key` header (any unique string, e.g. a UUID). The same key and request return the first answer, status and headers included. The key is scoped to the caller. Reuse it only to repeat a request after a timeout or a 5xx. |
| Concurrency | Every single resource is returned with an `ETag`. `PATCH`, `DELETE`, submit, cancel and resolution require it back in `If-Match` (412 when stale, 428 when missing). The ETag is a hash of the representation, so it changes when, and only when, something the client can see has changed. |
| Conditional reads | `If-None-Match` with the held ETag answers 304 without a body. Used for polling a run, its execution and a job. |
| Paging | `limit` (1 to 100, default 50) and an opaque `cursor`; the next one is `nextCursor`, `null` on the last page. Order is newest first. |
| Names | JSON members are the workbook prop names in camelCase. Dates `YYYY-MM-DD`, timestamps ISO 8601 with offset. A select value is the option's `value` (`north-america`, `chain-ladder`), not its label. **Every member is always present**; one without a value is `null`. |
| Versioning | The major version is in the path (`/gea/v1`). Additive changes keep it; a breaking change is `/gea/v2`. |
| Roles | A user has one role and a role is three permissions: `prepare`, `review`, `administer`. Reading needs none. An operation that needs one names it in the contract (`x-permission`) and answers 403 `forbidden` to a caller without it, before it looks at the id, the body or the ETag. See [Roles and permissions](#roles-and-permissions). |
| CORS | The API exposes `ETag`, `Location`, `X-Request-Id` and `Retry-After` to browsers and accepts `If-Match`, `If-None-Match`, `Idempotency-Key`. |

## Routes

The column **Needs** is the permission the caller's role has to carry; empty means every signed-in user.

| Method and path | operationId | Purpose | Headers | Needs |
|---|---|---|---|---|
| `GET /health` | `getHealth` | Liveness and the contract format version (no auth) | |  |
| `GET /users/me` | `getMe` | The caller: home region, role and what the role allows (`permissions`) | `If-None-Match` |  |
| `PATCH /users/me` | `updateMe` | Change the caller's own home region | `If-Match` |  |
| `GET /users` | `listUsers` | The people a project can be given to, with role and home region; `role`, `region`, `q`; cursor paging | |  |
| `GET /users/{userId}` | `getUser` | One user, with the ETag to change them with | `If-None-Match` | `administer` |
| `PATCH /users/{userId}` | `updateUser` | Give a user a role, another home region, or both | `If-Match` | `administer` |
| **Reference data** | | | |  |
| `GET /roles` | `getRoles` | The roles and what each allows | |  |
| `GET /regions` | `getRegions` | `Project.Region`: the regions of the project form | |  |
| `GET /business-purposes` | `getBusinessPurposes` | `Project.BusinessPurpose` | |  |
| `GET /benefits` | `getBenefits` | `Project.Benefit` | |  |
| `GET /treaties` | `getTreaties` | The treaties a run can analyse; `region`, `q` | |  |
| `GET /datasets` | `getDatasets` | The datasets a run can use as its `dataScope` | |  |
| **Projects** | | | |  |
| `GET /projects` | `listProjects` | Filter by `businessPurpose`, `region`, `state`, `cycle`, `q`; cursor paging | |  |
| `POST /projects` | `createProject` | Create (`name`, `region`, `businessPurpose`, `benefit`) | `Idempotency-Key` | `prepare` |
| `GET /projects/{projectId}` | `getProject` | Project with its counters | `If-None-Match` |  |
| `PATCH /projects/{projectId}` | `updateProject` | Merge patch; region and business purpose are fixed | `If-Match` | `prepare` |
| `GET /projects/{projectId}/run-parameters` | `getRunParameters` | Every run field of sheet "POC Data" (display name, step, type, control, required) with the dropdown values and defaults that apply in this project; `investigation` narrows them | |  |
| **Runs** | | | |  |
| `GET /runs` | `listRuns` | Filter by `projectId`, `jobId`, `status`, `q`; cursor paging | |  |
| `POST /runs` | `createRun` | **The draft**: `projectId`, `name`, `treaty`; optionally `cloneSourceId` and any other field | `Idempotency-Key` | `prepare` |
| `GET /runs/{runId}` | `getRun` | The run: every field, the steps with what is missing, its job, contract, resolution | `If-None-Match` |  |
| `PATCH /runs/{runId}` | `updateRun` | **Save the wizard**: the fields that changed | `If-Match` | `prepare` |
| `DELETE /runs/{runId}` | `deleteRun` | Discard a never-submitted draft | `If-Match` | `prepare` |
| `GET /runs/{runId}/review` | `reviewRun` | Review: `ready`, every missing field, contract preview; ETag of the run | |  |
| `POST /runs/{runId}/submit` | `submitRun` | **Submit** one run: freeze it into the next contract version and queue it for execution | `Idempotency-Key`, `If-Match` | `prepare` |
| **Execution** | | | |  |
| `GET /runs/{runId}/execution` | `getRunExecution` | Where the execution is: delivery, status, every step, last report | `If-None-Match` |  |
| `GET /runs/{runId}/logs` | `getRunLogs` | The execution log, newest first; cursor paging | |  |
| `POST /runs/{runId}/cancel` | `cancelRun` | Cancel a queued or running run | `If-Match` | `prepare` |
| `POST /runs/{runId}/resolution` | `resolveRun` | Resolve a failed run: `rerun-with-fixed-config` or `mark-resolved`, with a note | `If-Match` | `prepare` |
| **Jobs** | | | |  |
| `GET /jobs` | `listJobs` | Filter by `projectId`, `status`, `q`; cursor paging | |  |
| `POST /jobs` | `createJob` | Submit several runs together: `name`, `runIds` in execution order, `maxParallel` | `Idempotency-Key` | `prepare` |
| `GET /jobs/{jobId}` | `getJob` | Status, counters, progress, duration | `If-None-Match` |  |
| **Contracts** | | | |  |
| `GET /runs/{runId}/contracts` | `listRunContracts` | Contract versions of a run | |  |
| `GET /runs/{runId}/contracts/{version}` | `getRunContract` | One version | |  |
| `GET /contracts/{contractId}` | `getContract` | Contract by id with delivery state | |  |
| **Operations** | | | |  |
| `POST /operations/deliveries/{contractId}/retry` | `retryDelivery` | Send a contract to Snowflake again after its delivery gave up (202) | | `administer` |

### Relation to the workbook's "04. API Data" sheet

| Workbook | Here |
|---|---|
| `GET /projects` | Same route. `data` is `{ projects, nextCursor }`. Each project has the sheet's members: `id`, `name`, `businessPurpose`, `periodFrom`, `periodTo`, `state`, `description`, `locked`, `inheritedFrom`, `runs` (`count`, `completed`, `active`, `failed`, plus `draft` and `unresolvedFailed`), `jobs.count`, `analyses.count`, `portfolioAe`, `region`, `owner` (`id`, `name`), `createdAt`, `signedOffAt`; plus `benefit`, `cycle`, `updatedAt`, `revision`. |
| `POST /projects` | Same route. Body: the four `createProject` props of sheet "POC Data", optionally `cycle`, `periodFrom`, `periodTo`, `owner`, `description`, `inheritedFrom`, `state`. |
| `POST /runs` | Same route. Body: `projectId`, `name`, `treaty`; any other `createRun` prop may come with it. |
| `GET /run/{id}` | `GET /runs/{runId}`: one spelling for the collection and its members. |
| `GET /runs` | Same route. `data` is `{ runs, nextCursor }`. |
| `GET /profile` ("Get list of parameters") | `GET /projects/{projectId}/run-parameters`. "Profile" is the workbook's methodology profile, which a reader takes for the user's profile; the route says what it returns and whose it is. The lists of the project form, which were part of it, are `GET /regions`, `/business-purposes`, `/benefits`. |
| `GET /datasets` | Same route: `{ datasets: [{ id, name }] }`. |
| `GET /priorbasis`, `/claimmethods`, `/actuals`, `/enrichments` | Left out: they belong to the sheet 02/03 wizard, which is not the POC field set (W1, W10). |
| Job (sheet 03) | `POST /jobs`, `GET /jobs`, `GET /jobs/{jobId}`; `Run.JobId` is `jobId` on the run. |
| `Run.ResolutionAction` (sheet 03) | `POST /runs/{runId}/resolution`. |
| `Run.Steps`, `Run.Logs` (sheet 03) | `GET /runs/{runId}/execution`, `GET /runs/{runId}/logs`. |
| "the current user is not a Viewer" (sheet 02, Run details) | The permission `prepare`: `permissions.prepare` in `GET /users/me`, required by every operation that changes a project or a run. |
| (not in the workbook) | `PATCH /runs/{id}`, review, submit, cancel, contracts, `/treaties`, `/users`, `/roles`, `/health`, `/operations`. |

Not there yet, and answered with a fixed value so that the shape is the sheet's: `analyses.count` is `0` and
`portfolioAe` is `null` (no Analysis or result tables exist). `GET /treaties` is empty until treaties are loaded
from the source system; a run accepts any treaty text until then.

## The run

One resource carries the whole wizard. The props are flat, under the workbook names; `steps` only says how the
wizard groups them and what is missing where (shortened: all 32 props, 8 steps and every issue are returned):

```json
{
  "id": "6f0c...", "projectId": "a1b2...", "name": "Mortality 2016-2026", "treaty": "TRT-001",
  "region": "north-america", "status": "draft", "locked": false, "cloneSourceId": null,
  "dataScope": ["Policy_v1", "Claims_v1"],
  "studyPeriod": { "start": "2016-01-01", "end": "2026-01-01" },
  "studyPeriodExclusions": null,
  "studyPeriodTreatyOverride": false,
  "perTreatyEndDatesMapping": null,
  "investigation": "mortality",
  "exposureMethod": null,
  "...": "every other createRun prop, null while empty",
  "steps": [
    { "key": "main", "title": "Main", "ordinal": 0, "status": "complete", "missing": [] },
    { "key": "dataAndSetUp", "title": "Data and set up", "ordinal": 1, "status": "complete", "missing": [] },
    { "key": "segmentation", "title": "Segmentation", "ordinal": 2, "status": "incomplete", "missing": ["exposureMethod", "initialExposureMethod"] }
  ],
  "issues": [
    { "code": "required", "message": "Exposure Method is required.", "field": "exposureMethod", "stepKey": "segmentation", "pointer": "/exposureMethod" }
  ],
  "ready": false,
  "currentContract": null, "submittedAt": null, "submittedBy": null, "failureMessage": null,
  "jobId": null, "cancelRequestedAt": null, "resolution": null,
  "revision": 3, "createdAt": "2026-10-02T09:00:00+00:00", "updatedAt": "2026-10-02T09:04:10+00:00"
}
```

## Saving in the wizard

The run is a server-side draft from the first step, so a wizard can be left and resumed on another device,
and nothing depends on the browser keeping state.

| Moment | Call | Why |
|---|---|---|
| Open the form | `GET /projects/{id}/run-parameters` | Labels, controls, required flags, dropdown values and defaults of every field for this project. |
| Step "main" (name, treaty) | `POST /runs` with `Idempotency-Key` | Creates the draft. A double click or a retry after a timeout cannot create two runs. |
| The user edits a field | `PATCH /runs/{id}` with `If-Match`, debounced | Only the changed props travel: `{ "exposureMethod": "central" }`. Changes are collected and one save is in flight at a time, so saves cannot overtake each other. `null` empties a prop. |
| The user presses Next | `PATCH /runs/{id}` with what is still unsent | The same call, sent at once instead of after the pause. |
| Resume | `GET /runs/{id}` | Continue at the first step whose `status` is not `complete`. |
| Review | `GET /runs/{id}/review` | Every missing prop with its step, a preview of the contract, and the run's ETag. |
| Submit | `POST /runs/{id}/submit` with `Idempotency-Key` and `If-Match` (the review's ETag) | The contract is exactly what was reviewed: any save in between changes the run's ETag and the submit answers 412. |

Rules the server keeps:

- **Saving a draft never fails because the user is not finished.** A run with required props still empty is
  stored and answered with 200, `ready: false` and the missing props in `issues` and in `steps[].missing`.
  A save is rejected (422, nothing stored) only for a prop that does not exist or a value of the wrong type;
  the error then lists only those issues.
- **One ETag for the whole run.** Every save answers with the new one. A save with a stale ETag (another tab,
  another user) answers 412 and stores nothing.
- **The server decides completeness**, from the workbook's required column (`gea_api/fields.py`, generated),
  and the database repeats it at submit (`CK_Run_RequiredWhenSubmitted`). The client marks required inputs
  from `REQUIRED_FIELDS` as a hint only.
- **Dropdown values are offered, not enforced.** A run select accepts any text until the real lists exist
  (open question 3). Region, business purpose and benefit of a project are checked.
- **After submit the run is locked**: every write answers 409 `locked`. Change it by cloning, or resolve it
  after a failure.

## After submit: execution, jobs, decisions

The API does not execute anything. It stores the contract, lets PostgreSQL decide when it may start, and reads
what Snowflake reports ([../EXECUTION.md](../EXECUTION.md)).

| Moment | Call | What the answer says |
|---|---|---|
| Submit several runs | `POST /jobs` `{ name, runIds, maxParallel }` | All or none. The order of `runIds` is the order of execution; `maxParallel: 1` is one after another, omitted is no limit. A run that is not a complete draft refuses the whole job (422 `not_ready`, one issue per run and field, pointer `/runIds/2`). |
| Watch a run | `GET /runs/{id}/execution` with `If-None-Match` | `status`; `delivery` (why it has not started: its turn in the job, a retry); every step with status, times and what it reported; `lastReportedAt` (heartbeat). |
| Watch a job | `GET /jobs/{id}` with `If-None-Match`, then `GET /runs?jobId=` | `status` (`queued`, `running`, `complete`, `completed-with-failures`, `failed`), counters, `progressPercentage`, `durationSeconds`. The ETag moves when a run of the job moves. |
| Read what happened | `GET /runs/{id}/logs` | Lines from Snowflake and from here (cancel, an execution that went silent). |
| Cancel | `POST /runs/{id}/cancel` with `If-Match` | Not sent yet: the run is `failed` at once. Under way: `cancelRequestedAt` is set and the run fails when Snowflake has stopped. |
| Resolve a failure | `POST /runs/{id}/resolution` `{ action, note }` with `If-Match` | `rerun-with-fixed-config`: the run is a draft again and the next submit is version n+1. `mark-resolved`: it stays failed, with who accepted it and why. |

A run in a job:

```json
{ "id": "...", "name": "Mortality batch", "kind": "data", "note": "Q3 refresh", "projectId": "...", "projectName": "Europe 2026",
  "status": "running", "maxParallel": 1,
  "runs": { "count": 3, "completed": 1, "failed": 0, "running": 1, "queued": 1 },
  "progressPercentage": 33.3, "durationSeconds": null, "finishedAt": null,
  "submittedBy": { "id": "...", "name": "tester" }, "submittedAt": "2026-10-02T09:00:00+00:00" }
```

## Roles and permissions

The workbook has no list of roles (README, W15). Decision 21: a user has one role, and a role is a row of
`gea."Role"` whose three columns say what it allows.

| Role | `prepare` | `review` | `administer` | In words |
|---|---|---|---|---|
| `viewer` | | | | Reads everything, changes nothing. The role of a user seen for the first time. |
| `preparer` | yes | | | Creates and changes projects and runs, submits, cancels, resolves a failure, clones. |
| `reviewer` | yes | yes | | The same, and signs off. |
| `admin` | yes | yes | yes | The same, and gives users their role and home region; retries a delivery. |

| Permission | Required by |
|---|---|
| none | Every `GET` except `GET /users/{userId}`; `PATCH /users/me` (the caller's own home region). |
| `prepare` | `POST /projects`, `PATCH /projects/{id}`, `POST /runs` (also a clone), `PATCH` and `DELETE /runs/{id}`, `POST /runs/{id}/submit`, `/cancel`, `/resolution`, `POST /jobs`. |
| `review` | No operation yet: Basis, Analysis and sign-off are not built. The database already requires it of `Project.SignedOffBy` (SQLSTATE `GEA06`). |
| `administer` | `GET` and `PATCH /users/{userId}`, `POST /operations/deliveries/{contractId}/retry`. |

```http
GET /gea/v1/users/me

{ "data": { "id": "...", "name": "Ana", "email": null, "region": "europe", "regionName": "Europe",
            "role": "preparer", "roleName": "Preparer",
            "permissions": { "prepare": true, "review": false, "administer": false } },
  "error": null }
```

```http
POST /gea/v1/projects            (as a Viewer)

HTTP/1.1 403 Forbidden
{ "data": null,
  "error": { "code": "forbidden", "status": 403, "issues": [], "requestId": "...",
             "message": "Your role (Viewer) does not allow this. It takes a role that may prepare projects and runs." } }
```

- **Ask for the permission, not for the role.** A form shows an action when `permissions.prepare` is true. Role
  names are for display. A fifth role, or a change to what a role allows, is then a row in `gea."Role"` and
  neither the API nor the client changes.
- **The check comes first.** A caller without the permission gets 403 whatever the id, the body or the ETag
  is, so the answer says nothing about what exists.
- **A change of role applies to the next request.** The role is read with every request; there is nothing to
  sign out of. It is recorded in the audit log (`user.role_changed`, from, to, by whom).
- **Nobody changes their own role** (403), so an Admin cannot lock themselves out by mistake.
- **The first Admin.** A new user is a Viewer unless the identity provider names a role. For a new
  installation either the provider's claim makes someone `admin`, or the database administrator runs
  `UPDATE gea."User" SET "RoleId" = 'admin' WHERE "Subject" = '<subject>'` once.
- **`Project.Owner` grants nothing.** It is the user responsible for the project, as in the workbook; whether
  an owner may do more than a collaborator is the workbook's open question (sheet 05, question 5).

## Implementation notes (`gea_api`)

```text
gea_api/
  app.py          create_app(): Litestar, CORS, the three exception handlers (ApiError, HTTPException, Exception)
  routes.py       the handlers; respond() = authenticate, read the caller's role, one transaction, answer in the envelope
  services.py     one function per operation: @needs(permission), idempotency, lock, If-Match, validate, write
  fields.py       GENERATED from spec/poc-data.json: the props, their columns, types, steps, required flags
  runconfig.py    the run fields: validate a body, build the UPDATE of exactly the columns that changed, list what is missing
  queries.py      the SQL; reads return API-shaped JSON built by PostgreSQL
  errors.py       ApiError (the `error` of the envelope) and the SQLSTATE mapping
  http.py         ETag, If-Match / If-None-Match, merge patch, cursors, canonical JSON
  validation.py   shape of request bodies; every issue at once, with JSON Pointers
  auth.py         Authenticator plug-in point; DevAuthenticator for local use; the caller's row, role and permissions
  db.py           psycopg 3 connection pool, transaction()
  config.py       settings from the environment
```

```bash
export GEA_DATABASE_URL=postgresql://gea_api:secret@localhost:5432/gea     # a member of role gea_app
export GEA_AUTH_MODE=dev                                                   # local only, see auth.py
python -m gea_api --port 8010
```

Every write follows the same order inside one transaction: refuse a caller whose role lacks the permission
the operation needs; replay a stored answer when the
`Idempotency-Key` is known; lock the row; read the current representation; compare `If-Match` with its
ETag; validate; write; answer with the new representation and ETag. An `ApiError` raised anywhere rolls the
transaction back.

- **Reads return JSON built in SQL** (`jsonb_build_object`), so there is one mapping from tables to the API,
  and it can be run in psql. Every parameter is cast in the SQL (`%(id)s::uuid`).
- **Saving a run** is one `UPDATE gea."Run" SET "<Column>" = ... WHERE "Id" = ...` with only the columns of the
  props in the request; the column names come from the generated field list, never from the request.
- **Locking.** Every write to a run takes `SELECT ... FOR UPDATE` on the run row first, so a save and a submit
  cannot interleave; the excluded periods take `FOR SHARE` on it in their trigger.
- **Two requests with the same Idempotency-Key at once**: the second loses on the receipt's primary key, is
  rolled back, and is answered from the stored receipt.
- **Authentication** is a plug-in point because the identity provider is not decided (open question 7).
  Without an authenticator the application refuses to start; `GEA_AUTH_MODE=dev` trusts an `X-Dev-User`
  header and is for local development only. In dev mode a user seen for the first time gets the role in
  `X-Dev-Role`, and `admin` without it, so that a developer can do everything.
- **Permissions.** `services.needs("prepare")` marks an operation; `services.PERMISSIONS` and the contract's
  `x-permission` are compared by `tests/gea/test_api.py`, which also calls every such operation as a caller
  without the permission and expects 403. `tools/check_openapi.py` refuses an operation that changes something
  and names no permission.
- **Home region.** A caller seen for the first time becomes a `gea."User"` row in the region the authenticator
  reports (`X-Dev-Region` in dev mode), or `global` when it reports none. `GET /users/me` returns it so that
  the project form can preselect it; `PATCH /users/me` changes it.

### PostgreSQL error to HTTP

The triggers raise their own SQLSTATE values, so the mapping needs no message parsing (`errors.py`):

| SQLSTATE | Raised when | HTTP | `code` |
|---|---|---|---|
| `GEA03` | run or project is locked, contract already published, immutable row | 409 | `locked` |
| `GEA04` | illegal status transition; resolving a run that has not failed; cancelling one that is not under way | 409 | `invalid_state` |
| `GEA05` | the contract document no longer matches the saved configuration | 412 | `precondition_failed` |
| `GEA06` | the user's role does not allow this: a project signed off by a user whose role may not review | 403 | `forbidden` |
| `23514` on `CK_Run_RequiredWhenSubmitted` | a required prop is empty at submit (the API reports the props itself first; this is the backstop) | 422 | `not_ready` |
| `23503`, `23514`, `23502`, `22P02`, `22007`, `22008` | unknown reference, a plain CHECK failed, unparsable value | 422 | `validation_failed` |
| `23505` | a unique rule other than the idempotency receipt | 409 | `invalid_state` |
| `40001`, `40P01`, `55P03`, `57014`, class `08` | serialisation failure, deadlock, lock or statement timeout, connection | 503 | `service_unavailable` |
| anything else | | 500 | `internal_error` |

### Submit, in SQL

```sql
SELECT ... FROM gea."CommandReceipt" WHERE "CreatedBy" = $user AND "IdempotencyKey" = $key;   -- found: replay
SELECT ... FROM gea."Run" WHERE "Id" = $1 FOR UPDATE;                    -- lock, then compare If-Match
-- Python: any required prop empty -> 422 not_ready with the issues
SELECT gea."BuildContractDocument"($1, $contract_id, $user, now());      -- assemble from the run's columns
-- Python: text = canonical_json(document); hash = sha256(text.encode()).hexdigest()
INSERT INTO gea."DataContract" (...) VALUES (...);  -- triggers verify, queue the Snowflake delivery, move the run to 'queued'
INSERT INTO gea."AuditEvent" (...); INSERT INTO gea."CommandReceipt" (...);
COMMIT;
```

`canonical_json` is `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, the same
as `calculation_api/models.py`. The hash covers exactly that text, the text is stored in `"DocumentCanonical"`,
and the same text is sent to Snowflake, where `SHA2(DOCUMENT_CANONICAL, 256)` must give the same value.

### Not built: the relay (role `gea_relay`)

A small worker that carries messages between PostgreSQL and Snowflake. It computes nothing and waits for
nothing; every call is short and can be repeated. Its duties in Snowflake are listed at the end of
`db/snowflake/V002__run_pipeline.sql`; in PostgreSQL it calls only these:

```sql
-- start what may start: runs on their own at once, runs of a job in order and MaxParallel at a time
SELECT * FROM gea."ClaimContractDeliveries"('relay-1', 10, interval '2 minutes');
-- per row: MERGE into GEA.CONTROL.DATA_CONTRACT, read CONTENT_HASH back, EXECUTE TASK ... USING CONFIG, then
SELECT gea."CompleteContractDelivery"($contract_id, 'snowflake', 'relay-1', $query_id);
-- or
SELECT gea."FailContractDelivery"($contract_id, 'snowflake', 'relay-1', $error, interval '30 seconds', 8);

-- observe: what changed in GEA.CONTROL.RUN_STATUS / RUN_STEP_STATUS / RUN_LOG
SELECT gea."RecordExecutionStatus"($contract_id, $status, $steps_json, $failure_message);   -- also the heartbeat
SELECT gea."RecordExecutionLog"($contract_id, $lines_json);                                 -- idempotent per line id
SELECT gea."FailStaleExecutions"(interval '30 minutes');                                    -- after a poll in which Snowflake answered

-- pass on: cancel requests of runs that are under way
SELECT "ContractId", "RequestedByName" FROM gea."RunCancelRequest";
```

## Vue client

See [../frontend/README.md](../frontend/README.md). Every function resolves to an `ApiResult` and never
throws; a failure carries the server's error object as `error`:

```ts
import { useRunWizard } from "@/composables/useRunWizard";

const wizard = useRunWizard(projectId);
const started = await wizard.start({ name: "Mortality 2016-2026", treaty: "TRT-001" });   // POST /runs
if (!started.ok) show(started.error.message);                    // also in wizard.error

wizard.change({ dataScope: ["Policy_v1"] });                     // autosave: PATCH /runs/{id} after 800 ms of quiet
wizard.saveState.value;                                          // "pending" | "saving" | "saved" | "error" | "conflict"
wizard.fieldErrors("dataAndSetUp");                              // { studyPeriod: ["Study Period is required."] }

await wizard.save({ investigation: "mortality" });               // Next: send now, with what is still waiting
const review = await wizard.loadReview();                        // ready? missing props, contract preview
const published = await wizard.submit();                         // 412 if anything changed since the review
wizard.pollUntilFinished();                                      // run and execution, If-None-Match, until complete or failed
wizard.execution.value?.steps;                                   // [{ stepKey, status, startedAt, finishedAt, detail }]
await wizard.cancel();                                           // queued or running
await wizard.resolve("rerun-with-fixed-config");                 // after a failure: back to draft
```

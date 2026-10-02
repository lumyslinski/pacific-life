# Vue 3 client for the GEA API

A thin, typed adapter: no business rules, no formulas. Types come from the OpenAPI contract
(`openapi-typescript`), calls go through `openapi-fetch`.

| File | Role | Edit by hand? |
|---|---|---|
| `src/api/generated.ts` | Types of every path, operation and schema | No. `python gea/tools/generate.py` (or `npm run generate:api`) |
| `src/api/fields.ts` | The field list of workbook sheet "POC Data": `STEP_KEYS`, `STEP_TITLES`, `RunProp`, `RUN_FIELDS`, `STEP_FIELDS`, `REQUIRED_FIELDS` | No. `python gea/tools/generate.py` |
| `src/api/client.ts` | The one `openapi-fetch` instance, bearer token hook, `ApiResult`, `ApiError`, idempotency keys | Yes |
| `src/api/gea.ts` | One function per API operation, each resolving to an `ApiResult` | Yes |
| `src/composables/useProjects.ts` | Projects list and Create project | Yes |
| `src/composables/useRunWizard.ts` | Run wizard: start, autosave, review, submit; then execution, cancel, resolution of a failure | Yes |
| `src/composables/useJobs.ts` | `useJobs`: list jobs, submit several runs as one job. `useJobMonitor`: one job and its runs, kept current | Yes |
| `src/composables/useCurrentUser.ts` | `useCurrentUser`: the signed-in user, the role and `can(permission)`. `useUsers`: the user list, the roles, giving a user a role | Yes |

## One result object

The API answers every request with one envelope, `{ data, error }` (see [../api/README.md](../api/README.md)).
Every function in `gea.ts`, and every action of the composables, resolves to that envelope plus the HTTP
status, and never throws. There is one error path, whatever went wrong:

```ts
type ApiResult<T> =
    | { ok: true;  status: number; data: T;    error: null; etag?: string; location?: string }
    | { ok: false; status: number; data: null; error: ApiError };

interface ApiError {
    code: string;          // "validation_failed", "precondition_failed", "locked", "network_error", ...
    message: string;       // the sentence to show the user
    status: number;        // HTTP status; 0 when no answer arrived
    issues: Issue[];       // one per field: { field, code, message, stepKey?, pointer? }
    requestId?: string;    // quote to support
    retryable: boolean;    // no connection or 5xx: the same request may be sent again
}
```

```ts
const result = await createProject(form);
if (!result.ok) {
    error.value = result.error;                          // result.error.message for the banner
    fields.value = fieldMessages(result.error.issues);   // { name: ["name is required."], ... } for the inputs
    return;
}
router.push(`/projects/${result.data.id}`);
```

`code`, `message`, `status`, `issues` and `requestId` are the server's `error` object unchanged; the client
only adds `retryable`. A dropped connection (`network_error`, status 0) and an answer that is not from the
API at all (`unexpected_response`, e.g. a gateway's HTML page) are given the same shape, so they arrive this
way. The composables additionally keep the last error in reactive state: `error`, and for the wizard's
saves `saveError`.

## The wizard: `useRunWizard(projectId)`

The run is one object, `run.value`, with the workbook's props on it (`run.value.dataScope`,
`run.value.studyPeriod`, `run.value.ibnrMethodology` ...). The steps are only how the form groups them
(`STEP_FIELDS`); there is one save call and one ETag for the whole run.

| Call | What it does |
|---|---|
| `start({ name, treaty, cloneSourceId? })` | `POST /runs`: creates the draft run in the project and loads the dropdown values (`GET /projects/{id}/run-parameters`). |
| `open(runId)` | Resumes a run. Continue at `firstIncompleteStep`. |
| `change(props)` | **Autosave.** Collects the changed props and sends one `PATCH /runs/{id}` after 800 ms without edits (`autosaveDelayMs`). `null` empties a prop. |
| `save(props?)` | **Next.** Sends now, together with whatever is still waiting to be autosaved. |
| `flush()`, `flushAll()` | Send collected changes now; wait until every change has been answered. |
| `fieldErrors(step?)` | Messages per prop: what the server refused, then what is still missing. Optionally for one step. |
| `parameter(prop)` | The description of a prop from the run parameters: display name, control, required, `options`, `default`. |
| `loadReview()` | Waits for pending saves, then loads readiness, the missing props and the contract preview. |
| `submit()` | `POST /runs/{id}/submit`: freezes exactly what was reviewed as the data contract and queues the run. |
| `refreshExecution()` | Loads `execution`: delivery, the status of every step, when Snowflake last reported, a cancel request. |
| `pollUntilFinished()`, `stopPolling()` | Polls the run and its execution with `If-None-Match` until the run is complete or failed. |
| `cancel()` | `POST /runs/{id}/cancel`. Before the contract was sent the run fails at once; afterwards the request is recorded (`run.cancelRequestedAt`) and Snowflake stops before its next step. |
| `resolve(action, note?)` | The decision on a failed run: `"rerun-with-fixed-config"` returns it to draft (the wizard is editable again), `"mark-resolved"` accepts the failure. |
| `dispose()` | Stops polling and sends what is still collected. Called automatically when the component (or effect scope) that created the wizard is unmounted. |

State: `run`, `parameters`, `review`, `contract`, `execution`, `busy`, `error`, `saveError`, `saveState`
(`"pending" | "saving" | "saved" | "error" | "conflict"`), `isLocked`, `firstIncompleteStep`,
`hasUnsavedChanges`, `canSubmit`; constants `stepKeys`, `stepFields`.

```vue
<select :value="wizard.run.value?.ibnrMethodology ?? ''"
        @change="wizard.change({ ibnrMethodology: $event.target.value || null })">
  <option v-for="o in wizard.parameter('ibnrMethodology')?.options ?? []" :key="o.value" :value="o.value">{{ o.label }}</option>
</select>
```

Behaviour worth knowing:

- **A half-filled run is not an error.** It is saved; the server answers with `ready: false`, the missing
  props in `run.issues` and per step in `run.steps[].missing`. `error` stays empty; `fieldErrors(step)` lists
  what is missing. Every prop is nullable in TypeScript for the same reason. `REQUIRED_FIELDS` in `fields.ts`
  is only a hint for marking inputs.
- **Saves never overtake each other.** One request is in flight; changes made meanwhile wait and go out as
  one `PATCH`.
- **The ETag is handled inside.** The composable keeps the run's ETag and sends `If-Match`. A component
  never sees it.
- **412 means the run was changed elsewhere** (another tab, another user). The composable loads the server's
  version into `run`, sets `saveState` to `"conflict"` and `saveError`, and leaves the decision to the form:
  show the other version, or call `save(props)` again to store the user's.
- **Submit is tied to the review.** `submit()` sends the ETag that came with the review. If anything was saved
  since, the server answers 412 and the review is loaded again, so the user confirms what will be frozen.
- **Idempotency keys are kept across retries.** `useProjects.create`, `start` and `submit` reuse the same
  `Idempotency-Key` when the same payload is sent again after a failure that is `retryable`, so a retry
  cannot create a second project, run or contract. The key is renewed after success, after a 4xx, or when
  the payload changes.
- **A transport error while polling is not a failed run.** Polling continues on `retryable` errors and
  stops on a 4xx or a terminal status.
- **After submit the run is locked** (`locked`, 409 on every write). Offer Clone (`start` with
  `cloneSourceId`), Cancel while it is queued or running, and the two resolutions when `status === "failed"`.
- **Stopping the poll is final.** `stopPolling()` (and `dispose()`) also stops a request that is under way
  from scheduling the next one.
- **Select values are option values** (`"chain-ladder"`); labels come from `parameter(prop).options`. The
  dropdown values depend on the project and on the selected investigation; the run parameters are loaded
  again when the investigation changes.
- **Lists are named after their content**: `listProjects` resolves to `{ projects, nextCursor }`, `listRuns`
  to `{ runs, nextCursor }`, `listJobs` to `{ jobs, nextCursor }`, `getRunLogs` to `{ logs, nextCursor }`;
  `nextCursor` is `null` on the last page.

## Jobs: `useJobs()` and `useJobMonitor(jobId)`

A job is several draft runs submitted together, all or none. It only says when its runs may start: in the
order of `runIds`, `maxParallel` at a time (`1` = one after another, omitted = all at once). One run is
submitted with the wizard and has no job.

```ts
const jobs = useJobs();
const created = await jobs.submit({ name: "Q3 mortality", note: null, runIds, maxParallel: 2 });
if (!created.ok) {
    // 422 not_ready: created.error.issues names every run that is not a draft or still misses a field
    return;
}
const monitor = useJobMonitor(created.data.id);
monitor.pollUntilFinished();          // monitor.job.value.status, .runs.count / .completed / .failed, .progressPercentage
                                      // monitor.runs.value: the runs in the order they execute
```

| | |
|---|---|
| `useJobs()` | `jobs`, `nextCursor`, `loading`, `error`; `load(filters?)`, `loadMore()`, `submit(job)` |
| `useJobMonitor(jobId)` | `job`, `runs`, `error`; `refresh()`, `pollUntilFinished(intervalMs = 5000)`, `stopPolling()` |

The poll is a 304 until a run of the job moves; only then are the runs loaded again. `submit` keeps its
`Idempotency-Key` across a retry, so a repeated request cannot submit the runs twice.

## Roles: `useCurrentUser()` and `useUsers()`

A user has one role, and a role is three permissions: `prepare`, `review`, `administer` (see
[../api/README.md](../api/README.md#roles-and-permissions)). A form asks for the permission, never for the role:

```vue
<script setup lang="ts">
const me = useCurrentUser();
void me.load();                                     // once, at start-up; again after a 403
</script>

<template>
  <button v-if="me.can('prepare')" @click="wizard.submit()">Submit</button>
  <span v-else>{{ me.user.value?.roleName }}: read only</span>
</template>
```

| | |
|---|---|
| `useCurrentUser()` | `user`, `loading`, `error`; `load()`, `can(permission)`, `isReadOnly`, `setHomeRegion(region)` |
| `useUsers()` | `users`, `roles`, `nextCursor`, `loading`, `error`; `load(filters?)`, `loadMore()`, `loadRoles()`, `change(userId, { role?, region? })` |

- **`can("prepare")` is the workbook's "the current user is not a Viewer"**: the condition on Re-run, Mark
  resolved, Clone and the other actions of the Run details view. `can()` is false until the user is loaded.
- **It decides what to show, not what is allowed.** The server checks every request and answers 403
  `forbidden` with a sentence that names the role; it arrives as an ordinary failure result
  (`result.error.code === "forbidden"`), like every other error.
- **`Permission` is a type** (`"prepare" | "review" | "administer"`), taken from the contract: `can("delete")`
  does not compile. Role ids are strings, because roles are rows of the server (`GET /roles`).
- **`useUsers().load()` is the owner picker** of the project form (`Project.Owner`): every signed-in user may
  read it, and it carries no e-mail address. `change()` needs `administer`; it reads the user first, so the
  change is made against what is stored now. Nobody changes their own role (403).

## First run

```bash
cd gea/frontend
npm install
npm run typecheck
```

Type-checked (strict) and run end to end against the API with `openapi-fetch` 0.17.0, `vue` 3.5.43,
`openapi-typescript` 7.13.0 and `typescript` 5.9.3. `openapi-typescript` 7 needs TypeScript 5.

`src/api/generated.ts` is written by `gea/tools/generate.py`, in the module shape of `openapi-typescript`
v7, so that the contract and the field list stay in step with one command and
`generate.py --check` can run without Node. `npm run generate:api` writes the same file with the official
tool; the client compiles against either.

Two things to know about `openapi-fetch`:

- It leaves members whose only type is `null` out of the types it returns, so the envelope's `error: null`
  is optional in `Raw<T>` (`client.ts`), and `deleteRun` goes through `toEmptyResult`.
- It keeps the `fetch` it finds when the client is created. A test that replaces `globalThis.fetch` later
  must pass `fetch` to `createClient`.

To use the client in the real application, copy `src/api` and `src/composables` into it (or make this folder
a workspace package) and set:

```bash
VITE_GEA_API_BASE_URL=http://localhost:8010/gea/v1     # or /api/gea/v1 behind a same-origin proxy
```

```ts
import { setAccessTokenProvider } from "@/api/client";
setAccessTokenProvider(() => auth.getAccessToken());     // once, at start-up
```

A browser on another origin can only read `ETag`, `Location` and `X-Request-Id` because the API exposes
them (CORS). Behind a proxy or gateway, make sure it passes those headers through, and `If-Match`,
`If-None-Match` and `Idempotency-Key` on the way in.

## When the field list changes

Edit `gea/spec/poc-data.json`, add the column in a new Alembic revision, run `python gea/tools/generate.py`,
then `npm run generate:api`. A removed or retyped prop becomes a compile error in the forms that use it.

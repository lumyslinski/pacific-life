/**
 * One function per API operation. Every function resolves to an ApiResult and never rejects:
 * `data` (with the `etag` to send back when changing the resource) or `error` (a message for
 * the user and the issues per prop), exactly as in the server's envelope. Components and
 * composables import from here, never from openapi-fetch directly, so transport details stay
 * in one module.
 */
import {
    api, newIdempotencyKey, toConditionalResult, toEmptyResult, toResult, type Schemas,
} from "./client";
import type { operations } from "./generated";

type QueryOf<Op extends keyof operations> = NonNullable<operations[Op]["parameters"]["query"]>;

const MERGE_PATCH = { "Content-Type": "application/merge-patch+json" };
const ifNoneMatch = (etag?: string) => (etag ? { "If-None-Match": etag } : {});

// ---------------------------------------------------------------- health, users
export const getHealth = () => toResult(api.GET("/health"));

/**
 * The caller: `region` is the home region to offer first when creating a project, `permissions`
 * what the caller's role allows. Ask `permissions.prepare`, not the role, before offering an action.
 */
export const getMe = () => toResult(api.GET("/users/me"));

/** The caller's own home region. The role is changed by an Admin, with updateUser. */
export const updateMe = (etag: string, changes: Schemas["UpdateUserRequest"]) =>
    toResult(api.PATCH("/users/me", { params: { header: { "If-Match": etag } }, body: changes, headers: MERGE_PATCH }));

/** The people a project can be given to, with their role and home region. Every signed-in user may read it. */
export const listUsers = (query: QueryOf<"listUsers"> = {}) => toResult(api.GET("/users", { params: { query } }));

/** One user with the ETag to change them with. Needs the permission `administer`. */
export const getUser = (userId: string, etag?: string) =>
    toConditionalResult(api.GET("/users/{userId}", { params: { path: { userId }, header: ifNoneMatch(etag) } }));

/** Give a user a role, another home region, or both. Needs `administer`; nobody changes their own role (403). */
export const updateUser = (userId: string, etag: string, changes: Schemas["AdministerUserRequest"]) =>
    toResult(api.PATCH("/users/{userId}", {
        params: { path: { userId }, header: { "If-Match": etag } }, body: changes, headers: MERGE_PATCH,
    }));

// ---------------------------------------------------------------- reference data: the lists the forms choose from
/** The roles a user can have and what each allows. */
export const getRoles = () => toResult(api.GET("/roles"));

/** Project.Region: the values of createProject.region. */
export const getRegions = () => toResult(api.GET("/regions"));

export const getBusinessPurposes = () => toResult(api.GET("/business-purposes"));

export const getBenefits = () => toResult(api.GET("/benefits"));

/** The treaties a run can analyse; with `region`, those of that region and those of no region. */
export const getTreaties = (query: QueryOf<"getTreaties"> = {}) => toResult(api.GET("/treaties", { params: { query } }));

/** getDatasets: the datasets a run can use as its data scope. */
export const getDatasets = () => toResult(api.GET("/datasets"));

// ---------------------------------------------------------------- projects
export const listProjects = (query: QueryOf<"listProjects"> = {}) =>
    toResult(api.GET("/projects", { params: { query } }));

export const createProject = (project: Schemas["CreateProjectRequest"], idempotencyKey: string = newIdempotencyKey()) =>
    toResult(api.POST("/projects", { params: { header: { "Idempotency-Key": idempotencyKey } }, body: project }));

/** With `etag`, `data` is null when the project has not changed since. */
export const getProject = (projectId: string, etag?: string) =>
    toConditionalResult(api.GET("/projects/{projectId}", { params: { path: { projectId }, header: ifNoneMatch(etag) } }));

/** Merge patch: send only what changes; null clears an optional member. `etag` is the one the project was read with. */
export const updateProject = (projectId: string, etag: string, changes: Schemas["UpdateProjectRequest"]) =>
    toResult(api.PATCH("/projects/{projectId}", {
        params: { path: { projectId }, header: { "If-Match": etag } }, body: changes, headers: MERGE_PATCH,
    }));

/**
 * The createRun fields as they apply in a project: display name, step, control, required, and for a
 * dropdown the values offered and the pre-populated default. `investigation` narrows the values.
 */
export const getRunParameters = (projectId: string, query: QueryOf<"getRunParameters"> = {}) =>
    toResult(api.GET("/projects/{projectId}/run-parameters", { params: { path: { projectId }, query } }));

// ---------------------------------------------------------------- runs: the draft and its submission
export const listRuns = (query: QueryOf<"listRuns"> = {}) => toResult(api.GET("/runs", { params: { query } }));

/**
 * createRun: the draft. `projectId`, `name` and `treaty` are enough; any other prop can be sent now
 * or saved later with updateRun. With `cloneSourceId` the configuration of that run is copied first.
 */
export const createRun = (run: Schemas["RunCreate"], idempotencyKey: string = newIdempotencyKey()) =>
    toResult(api.POST("/runs", { params: { header: { "Idempotency-Key": idempotencyKey } }, body: run }));

/** With `etag`, `data` is null when nothing about the run has changed since (for polling). */
export const getRun = (runId: string, etag?: string) =>
    toConditionalResult(api.GET("/runs/{runId}", { params: { path: { runId }, header: ifNoneMatch(etag) } }));

/** Save the wizard: the props that changed; null empties a prop. `etag` is the one the run was read with. */
export const updateRun = (runId: string, etag: string, changes: Schemas["RunPatch"]) =>
    toResult(api.PATCH("/runs/{runId}", {
        params: { path: { runId }, header: { "If-Match": etag } }, body: changes, headers: MERGE_PATCH,
    }));

/** `data` is null on success. */
export const deleteRun = (runId: string, etag: string) =>
    toEmptyResult(api.DELETE("/runs/{runId}", { params: { path: { runId }, header: { "If-Match": etag } } }));

/** The result's `etag` is the run's: pass it to submitRun so that what is submitted is what was reviewed. */
export const reviewRun = (runId: string) =>
    toResult(api.GET("/runs/{runId}/review", { params: { path: { runId } } }));

/**
 * Submit one run: its configuration is frozen into the next contract version and queued for execution.
 * Resolves to that contract. To submit several runs together, use createJob.
 */
export const submitRun = (runId: string, etag: string, idempotencyKey: string = newIdempotencyKey()) =>
    toResult(api.POST("/runs/{runId}/submit", {
        params: { path: { runId }, header: { "Idempotency-Key": idempotencyKey, "If-Match": etag } },
    }));

// ---------------------------------------------------------------- execution: observe and decide
/**
 * Where the execution is: delivery to Snowflake, status, every step, the last report (heartbeat).
 * With `etag`, `data` is null when nothing has changed since (for polling).
 */
export const getRunExecution = (runId: string, etag?: string) =>
    toConditionalResult(api.GET("/runs/{runId}/execution", { params: { path: { runId }, header: ifNoneMatch(etag) } }));

/** The execution log of the run, newest first. */
export const getRunLogs = (runId: string, query: QueryOf<"getRunLogs"> = {}) =>
    toResult(api.GET("/runs/{runId}/logs", { params: { path: { runId }, query } }));

/**
 * Cancel a queued or running run. Resolves to the run: already `failed` when it had not been sent to
 * Snowflake yet, otherwise with `cancelRequestedAt` set until Snowflake has stopped.
 */
export const cancelRun = (runId: string, etag: string) =>
    toResult(api.POST("/runs/{runId}/cancel", { params: { path: { runId }, header: { "If-Match": etag } } }));

/** Resolve a failed run: "rerun-with-fixed-config" returns it to draft, "mark-resolved" accepts the failure. */
export const resolveRun = (runId: string, etag: string, resolution: Schemas["RunResolutionRequest"]) =>
    toResult(api.POST("/runs/{runId}/resolution", {
        params: { path: { runId }, header: { "If-Match": etag } }, body: resolution,
    }));

// ---------------------------------------------------------------- contracts
export const listRunContracts = (runId: string) =>
    toResult(api.GET("/runs/{runId}/contracts", { params: { path: { runId } } }));

export const getRunContract = (runId: string, version: number) =>
    toResult(api.GET("/runs/{runId}/contracts/{version}", { params: { path: { runId, version } } }));

export const getContract = (contractId: string) =>
    toResult(api.GET("/contracts/{contractId}", { params: { path: { contractId } } }));

// ---------------------------------------------------------------- jobs: several runs submitted together
export const listJobs = (query: QueryOf<"listJobs"> = {}) => toResult(api.GET("/jobs", { params: { query } }));

/**
 * Submit several draft runs as one job, all or none. The order of `runIds` is the order of execution;
 * `maxParallel` is how many execute at once (1 = one after another, omitted = no limit).
 */
export const createJob = (job: Schemas["JobCreate"], idempotencyKey: string = newIdempotencyKey()) =>
    toResult(api.POST("/jobs", { params: { header: { "Idempotency-Key": idempotencyKey } }, body: job }));

/** With `etag`, `data` is null when no run of the job has moved since (for polling). */
export const getJob = (jobId: string, etag?: string) =>
    toConditionalResult(api.GET("/jobs/{jobId}", { params: { path: { jobId }, header: ifNoneMatch(etag) } }));

// ---------------------------------------------------------------- operations
/** Send a contract to Snowflake again after its delivery gave up. */
export const retryDelivery = (contractId: string) =>
    toResult(api.POST("/operations/deliveries/{contractId}/retry", { params: { path: { contractId } } }));

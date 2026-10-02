import { computed, getCurrentScope, onScopeDispose, ref, shallowRef } from "vue";
import {
    createIdempotencyKeeper, fieldMessages, type ApiError, type ApiResult, type Schemas,
} from "../api/client";
import { STEP_FIELDS, STEP_KEYS, type RunProp, type StepKey } from "../api/fields";
import {
    cancelRun, createRun, getRun, getRunExecution, getRunParameters, resolveRun, reviewRun, submitRun, updateRun,
} from "../api/gea";

type Run = Schemas["Run"];
type RunPatch = Schemas["RunPatch"];
type RunParameter = Schemas["RunParameter"];

/** Where the autosave stands. */
export type SaveState = "saved" | "pending" | "saving" | "error" | "conflict";

export interface RunWizardOptions {
    /** Quiet time after the last change before an autosave is sent. */
    autosaveDelayMs?: number;
}

/**
 * State of the Run creation wizard (V-RCREATE).
 *
 *     start() -> change() while the user types, save() on Next -> loadReview() -> submit() -> pollUntilFinished()
 *
 * The run is a draft on the server from the first step. Its fields are the createRun.* props of
 * workbook sheet "POC Data" (`run.value.dataScope`, `run.value.studyPeriod` ...); the steps are
 * only how the wizard groups them (`STEP_FIELDS`).
 *
 * After submit nothing is computed here or in the API: Snowflake executes the steps. The wizard
 * observes (`execution`, refreshed while polling) and passes the user's decisions on: `cancel()`
 * and, after a failure, `resolve()`.
 *
 * How saving works:
 * - `change(props)` is the autosave. Changes are collected and sent as one PATCH after a short
 *   pause; only one save is in flight, the next one waits for it. `null` empties a prop.
 * - `save(props)` sends at once (on Next).
 * - The server decides what is missing. A half-filled run is saved and comes back with the missing
 *   props in `run.issues` and per step in `run.steps`; that is not an error.
 * - Every action resolves to an ApiResult and never throws. A failure is also put into `error`
 *   (and `saveError` for a save), with a message for the user and the issues per prop.
 * - 412 means the run was changed elsewhere. The composable loads the server's version, sets
 *   `saveState` to "conflict" and leaves the decision (take theirs, or send mine again) to the form.
 */
export function useRunWizard(projectId: string, options: RunWizardOptions = {}) {
    const autosaveDelayMs = options.autosaveDelayMs ?? 800;

    const run = shallowRef<Run>();
    /** The run fields with the values their dropdowns offer in this project (getRunParameters). */
    const parameters = shallowRef<Schemas["RunParameters"]>();
    /** Where the execution of the submitted run is: delivery, status, every step. */
    const execution = shallowRef<Schemas["RunExecution"]>();
    const review = shallowRef<Schemas["RunReview"]>();
    const contract = shallowRef<Schemas["DataContract"]>();
    const busy = ref(false);
    /** The error of the last action that failed; cleared by the next action that succeeds. */
    const error = shallowRef<ApiError>();
    /** The error of the last refused save. */
    const saveError = shallowRef<ApiError>();
    const saveState = ref<SaveState>("saved");

    const isLocked = computed(() => run.value?.locked ?? false);
    const firstIncompleteStep = computed<StepKey | undefined>(() =>
        run.value?.steps.find((step) => step.status !== "complete")?.key);
    const hasUnsavedChanges = computed(() => saveState.value === "pending" || saveState.value === "saving");
    const canSubmit = computed(() => (run.value?.ready ?? false) && !hasUnsavedChanges.value);

    // ETags are transport state, not something a template renders.
    let runEtag: string | undefined;
    let reviewEtag: string | undefined;
    let pending: RunPatch | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight: Promise<unknown> = Promise.resolve();
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let polling = 0;                                                // changes whenever polling is stopped or restarted
    let parametersFor: string | null | undefined;
    let executionEtag: string | undefined;

    const withStartKey = createIdempotencyKeeper();
    const withSubmitKey = createIdempotencyKeeper();

    const notStarted = (): ApiResult<never> => ({
        ok: false,
        status: 0,
        data: null,
        error: { code: "invalid_state", message: "Create or open a run first.", status: 0, issues: [], retryable: false },
    });

    async function track<T>(action: () => Promise<ApiResult<T>>): Promise<ApiResult<T>> {
        busy.value = true;
        const result = await action();
        busy.value = false;
        error.value = result.error ?? undefined;
        return result;
    }

    function store(current: Run, etag: string | undefined): void {
        run.value = current;
        runEtag = etag;
        reviewEtag = undefined;
        review.value = undefined;
    }

    /** The dropdown values depend on the project and on the investigation that is selected. */
    async function refreshParameters(current: Run): Promise<void> {
        if (parameters.value && parametersFor === current.investigation) return;
        const loaded = await getRunParameters(current.projectId, { investigation: current.investigation ?? undefined });
        if (loaded.ok) {
            parameters.value = loaded.data;
            parametersFor = current.investigation;
        }
    }

    async function loadContext(current: Run, etag: string | undefined): Promise<ApiResult<Run>> {
        store(current, etag);
        pending = undefined;
        saveState.value = "saved";
        saveError.value = undefined;
        parameters.value = undefined;
        execution.value = undefined;
        executionEtag = undefined;
        await refreshParameters(current);
        if (current.currentContract) await refreshExecution();
        return { ok: true, status: 200, data: current, error: null, etag };
    }

    /** Step "main": create the draft run (optionally from a clone source) and load its dropdown values. */
    function start(main: Omit<Schemas["RunCreate"], "projectId">): Promise<ApiResult<Run>> {
        return track(async () => {
            const body = { ...main, projectId };
            const created = await withStartKey(body, (key) => createRun(body, key));
            return created.ok ? loadContext(created.data, created.etag) : created;
        });
    }

    /** Continue an existing run (draft or locked). Resume at `firstIncompleteStep`. */
    function open(runId: string): Promise<ApiResult<Run>> {
        return track(async () => {
            const current = await getRun(runId);
            if (!current.ok) return current;
            return loadContext(current.data as Run, current.etag);
        });
    }

    async function send(changes: RunPatch): Promise<ApiResult<Run>> {
        const current = run.value;
        if (!current) return notStarted();
        saveState.value = "saving";
        const result = await updateRun(current.id, runEtag ?? "", changes);
        if (result.ok) {
            store(result.data, result.etag);
            saveError.value = undefined;
            if (!pending) saveState.value = "saved";
            await refreshParameters(result.data);
        } else {
            saveError.value = result.error;
            if (result.error.code === "precondition_failed") {
                // Changed elsewhere: show the server's version and let the user decide.
                const theirs = await getRun(current.id);
                if (theirs.ok && theirs.data) store(theirs.data, theirs.etag);
                saveState.value = "conflict";
            } else {
                saveState.value = "error";
                if (result.error.code === "locked") void refreshRun();
            }
        }
        error.value = result.error ?? undefined;
        return result;
    }

    /** One save at a time: the next one starts when the previous one has answered. */
    function enqueue(changes: RunPatch): Promise<ApiResult<Run>> {
        const next = inFlight.then(() => send(changes));
        inFlight = next;
        return next;
    }

    /** Send the collected changes now (for example before leaving the page). */
    function flush(): Promise<ApiResult<Run> | undefined> {
        if (timer !== undefined) clearTimeout(timer);
        timer = undefined;
        const changes = pending;
        if (!changes) return Promise.resolve(undefined);
        pending = undefined;
        return enqueue(changes);
    }

    /**
     * Autosave. Call it whenever props change; `null` empties a prop.
     * The save is sent after `autosaveDelayMs` without further changes.
     */
    function change(props: RunPatch): void {
        pending = { ...pending, ...props };
        saveState.value = "pending";
        if (timer !== undefined) clearTimeout(timer);
        timer = setTimeout(() => void flush(), autosaveDelayMs);
    }

    /** Save now (on Next), together with whatever is still waiting to be autosaved. */
    function save(props: RunPatch = {}): Promise<ApiResult<Run> | undefined> {
        pending = { ...pending, ...props };
        if (Object.keys(pending).length === 0) pending = undefined;
        return flush();
    }

    /** Wait until every change made so far has been answered by the server. */
    async function flushAll(): Promise<void> {
        await flush();
        await inFlight;
    }

    /** Messages per prop: what the server refused, then what is still missing. Optionally for one step. */
    function fieldErrors(step?: StepKey): Record<string, string[]> {
        const all = [...(saveError.value?.issues ?? []), ...(run.value?.issues ?? [])];
        const props: readonly string[] | undefined = step ? STEP_FIELDS[step] : undefined;
        return fieldMessages(props ? all.filter((issue) => issue.field !== undefined && props.includes(issue.field)) : all);
    }

    /** The description of a field of the run form: display name, control, required, dropdown values, default. */
    function parameter(prop: RunProp): RunParameter | undefined {
        return parameters.value?.parameters.find((entry) => entry.name === prop);
    }

    async function refreshRun(): Promise<ApiResult<Run | null>> {
        const current = run.value;
        if (!current) return notStarted();
        const latest = await getRun(current.id, runEtag);
        if (latest.ok) {
            if (latest.data) run.value = latest.data;
            runEtag = latest.etag ?? runEtag;
        }
        return latest;
    }

    /** Where the execution is. `data` is null when nothing has changed since the last look. */
    async function refreshExecution(): Promise<ApiResult<Schemas["RunExecution"] | null>> {
        const current = run.value;
        if (!current) return notStarted();
        const latest = await getRunExecution(current.id, executionEtag);
        if (latest.ok) {
            if (latest.data) execution.value = latest.data;
            executionEtag = latest.etag ?? executionEtag;
        }
        return latest;
    }

    /** Review step: readiness, every missing prop with its step, and a preview of the contract. */
    function loadReview(): Promise<ApiResult<Schemas["RunReview"]>> {
        return track(async () => {
            const current = run.value;
            if (!current) return notStarted();
            await flushAll();
            const result = await reviewRun(current.id);
            if (result.ok) {
                review.value = result.data;
                reviewEtag = result.etag;
            }
            return result;
        });
    }

    /**
     * Submit: freeze exactly what was reviewed into a contract and queue it for execution in
     * Snowflake. If anything changed after the review the server answers 412 and the review is
     * loaded again.
     * Safe to call again after a network error: the same Idempotency-Key is sent.
     */
    function submit(): Promise<ApiResult<Schemas["DataContract"]>> {
        return track(async () => {
            const current = run.value;
            if (!current) return notStarted();
            if (!reviewEtag) {
                const reviewed = await loadReview();
                if (!reviewed.ok) return reviewed;
            }
            const etag = reviewEtag ?? "";
            const published = await withSubmitKey([current.id, etag], (key) => submitRun(current.id, etag, key));
            if (published.ok) {
                contract.value = published.data;
                runEtag = undefined;
                executionEtag = undefined;
                await refreshRun();
                await refreshExecution();
            } else if (published.error.code === "precondition_failed" || published.error.code === "not_ready") {
                reviewEtag = undefined;
                const again = await reviewRun(current.id);
                if (again.ok) {
                    review.value = again.data;
                    reviewEtag = again.etag;
                }
                runEtag = undefined;
                await refreshRun();
            }
            return published;
        });
    }

    /** The latest ETag of the run, read fresh: decisions are taken against what is true now. */
    async function latestEtag(runId: string): Promise<ApiResult<string>> {
        const latest = await getRun(runId);
        if (!latest.ok) return latest;
        return { ok: true, status: latest.status, data: latest.etag ?? "", error: null };
    }

    /**
     * Cancel the execution. A run that was not sent to Snowflake yet fails at once; one that is under
     * way gets `cancelRequestedAt` and fails when Snowflake has stopped, so keep polling.
     */
    function cancel(): Promise<ApiResult<Run>> {
        return track(async () => {
            const current = run.value;
            if (!current) return notStarted();
            const etag = await latestEtag(current.id);
            if (!etag.ok) return etag;
            const cancelled = await cancelRun(current.id, etag.data);
            if (cancelled.ok) {
                run.value = cancelled.data;
                runEtag = cancelled.etag;
                await refreshExecution();
            }
            return cancelled;
        });
    }

    /**
     * Resolve a failed run. "rerun-with-fixed-config": the run is a draft again and the next submit
     * creates the next contract version. "mark-resolved": the failure is accepted, with a note.
     */
    function resolve(action: Schemas["ResolutionAction"], note?: string): Promise<ApiResult<Run>> {
        return track(async () => {
            const current = run.value;
            if (!current) return notStarted();
            const etag = await latestEtag(current.id);
            if (!etag.ok) return etag;
            const resolved = await resolveRun(current.id, etag.data, { action, note: note ?? null });
            if (!resolved.ok) return resolved;
            if (action === "rerun-with-fixed-config") return loadContext(resolved.data, resolved.etag);
            run.value = resolved.data;
            runEtag = resolved.etag;
            return resolved;
        });
    }

    function stopPolling(): void {
        polling += 1;                                               // a request that is under way must not re-arm the timer
        if (pollTimer !== undefined) clearTimeout(pollTimer);
        pollTimer = undefined;
    }

    /** Poll the run and its execution with If-None-Match until the run is complete or failed. */
    function pollUntilFinished(intervalMs = 3000): void {
        stopPolling();
        const mine = polling;
        const tick = async (): Promise<void> => {
            const status = run.value?.status;
            if (status !== "queued" && status !== "running") return;
            const latest = await refreshRun();
            if (!latest.ok && !latest.error.retryable) {
                // A 4xx will not get better by asking again; no connection or a 5xx will.
                error.value = latest.error;
                return;
            }
            await refreshExecution();                              // steps move while the run stays "running"
            if (mine === polling) pollTimer = setTimeout(tick, intervalMs);
        };
        pollTimer = setTimeout(tick, intervalMs);
    }

    // Inside a component (or an effectScope) the wizard cleans up after itself; elsewhere call dispose().
    const dispose = () => {
        stopPolling();
        void flush();                                            // do not lose the last keystrokes
    };
    if (getCurrentScope()) onScopeDispose(dispose);

    return {
        run, parameters, review, contract, execution, busy, error, saveError, saveState,
        isLocked, firstIncompleteStep, hasUnsavedChanges, canSubmit,
        stepKeys: STEP_KEYS, stepFields: STEP_FIELDS,
        start, open, change, save, flush, flushAll, fieldErrors, parameter, refreshRun,
        refreshExecution, loadReview, submit, cancel, resolve, pollUntilFinished, stopPolling, dispose,
    };
}

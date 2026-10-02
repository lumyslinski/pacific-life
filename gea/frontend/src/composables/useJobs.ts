import { getCurrentScope, onScopeDispose, ref, shallowRef } from "vue";
import { createIdempotencyKeeper, type ApiError, type ApiResult, type Schemas } from "../api/client";
import { createJob, getJob, listJobs, listRuns } from "../api/gea";

type Job = Schemas["Job"];
type Filters = NonNullable<Parameters<typeof listJobs>[0]>;

/**
 * Jobs: several runs submitted together (V-JLIST, batch submit).
 *
 * A job only says when its runs may start: in the order given, `maxParallel` at a time. The runs
 * execute in Snowflake; nothing here waits for them. Watch one with `useJobMonitor`.
 */
export function useJobs() {
    const jobs = shallowRef<Job[]>([]);
    const nextCursor = ref<string | null>(null);
    const loading = ref(false);
    /** The error of the last action, or undefined. For a refused job `error.value.issues` names every run and field. */
    const error = shallowRef<ApiError>();

    let filters: Filters = {};
    const withSubmitKey = createIdempotencyKeeper();

    async function track<T>(action: () => Promise<ApiResult<T>>): Promise<ApiResult<T>> {
        loading.value = true;
        const result = await action();
        loading.value = false;
        error.value = result.error ?? undefined;
        return result;
    }

    async function load(newFilters: Filters = {}) {
        filters = { ...newFilters, cursor: undefined };
        const result = await track(() => listJobs(filters));
        if (result.ok) {
            jobs.value = result.data.jobs;
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    async function loadMore() {
        if (!nextCursor.value) return undefined;
        const result = await track(() => listJobs({ ...filters, cursor: nextCursor.value ?? undefined }));
        if (result.ok) {
            jobs.value = [...jobs.value, ...result.data.jobs];
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    /**
     * Submit the runs as one job, all or none. The order of `runIds` is the order of execution.
     * Safe to call again after a network error: the same Idempotency-Key is sent, so the runs are
     * not submitted twice.
     */
    async function submit(job: Schemas["JobCreate"]) {
        const result = await track(() => withSubmitKey(job, (key) => createJob(job, key)));
        if (result.ok) jobs.value = [result.data, ...jobs.value];
        return result;
    }

    return { jobs, nextCursor, loading, error, load, loadMore, submit };
}

/**
 * One job with its runs, kept current by polling (V-JDETAIL).
 *
 * The job's ETag covers its status and counters, which are derived from its runs: the poll is a
 * 304 until a run moves, and only then are the runs loaded again.
 */
export function useJobMonitor(jobId: string) {
    const job = shallowRef<Job>();
    /** The runs of the job in the order they execute. */
    const runs = shallowRef<Schemas["RunSummary"][]>([]);
    const error = shallowRef<ApiError>();

    let etag: string | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polling = 0;                                                // changes whenever polling is stopped or restarted

    /** Load the job, and its runs when anything about it has changed. */
    async function refresh(): Promise<ApiResult<Job | null>> {
        const latest = await getJob(jobId, etag);
        if (!latest.ok) {
            error.value = latest.error;
            return latest;
        }
        error.value = undefined;
        if (latest.data) {
            job.value = latest.data;
            etag = latest.etag ?? etag;
            const all: Schemas["RunSummary"][] = [];
            let cursor: string | undefined;
            do {
                const page = await listRuns({ jobId, limit: 100, cursor });
                if (!page.ok) break;
                all.push(...page.data.runs);
                cursor = page.data.nextCursor ?? undefined;
            } while (cursor);
            runs.value = all.sort((a, b) => (a.jobOrdinal ?? 0) - (b.jobOrdinal ?? 0));
        }
        return latest;
    }

    function stopPolling(): void {
        polling += 1;                                               // a request that is under way must not re-arm the timer
        if (timer !== undefined) clearTimeout(timer);
        timer = undefined;
    }

    /** Poll until no run of the job is queued or running. */
    function pollUntilFinished(intervalMs = 5000): void {
        stopPolling();
        const mine = polling;
        const tick = async (): Promise<void> => {
            const latest = await refresh();
            if (!latest.ok && !latest.error.retryable) return;       // a 4xx will not get better by asking again
            const status = job.value?.status;
            if (status !== undefined && status !== "queued" && status !== "running") return;
            if (mine === polling) timer = setTimeout(tick, intervalMs);
        };
        timer = setTimeout(tick, 0);
    }

    if (getCurrentScope()) onScopeDispose(stopPolling);

    return { job, runs, error, refresh, pollUntilFinished, stopPolling };
}

import { ref, shallowRef } from "vue";
import { createIdempotencyKeeper, type ApiError, type ApiResult, type Schemas } from "../api/client";
import { createProject, listProjects } from "../api/gea";

type Project = Schemas["Project"];
type Filters = NonNullable<Parameters<typeof listProjects>[0]>;

/** Projects list (V-PLIST) and Create project (V-PCREATE). */
export function useProjects() {
    const projects = shallowRef<Project[]>([]);
    const nextCursor = ref<string | null>(null);
    const loading = ref(false);
    /** The error of the last action, or undefined. `error.value.issues` has the messages per prop. */
    const error = shallowRef<ApiError>();

    let filters: Filters = {};
    const withCreateKey = createIdempotencyKeeper();

    async function track<T>(action: () => Promise<ApiResult<T>>): Promise<ApiResult<T>> {
        loading.value = true;
        const result = await action();
        loading.value = false;
        error.value = result.error ?? undefined;
        return result;
    }

    async function load(newFilters: Filters = {}) {
        filters = { ...newFilters, cursor: undefined };
        const result = await track(() => listProjects(filters));
        if (result.ok) {
            projects.value = result.data.projects;
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    async function loadMore() {
        if (!nextCursor.value) return undefined;
        const result = await track(() => listProjects({ ...filters, cursor: nextCursor.value ?? undefined }));
        if (result.ok) {
            projects.value = [...projects.value, ...result.data.projects];
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    /** Safe to call again after a network error: the same Idempotency-Key is sent, so no duplicate is created. */
    async function create(project: Schemas["CreateProjectRequest"]) {
        const result = await track(() => withCreateKey(project, (key) => createProject(project, key)));
        if (result.ok) projects.value = [result.data, ...projects.value];
        return result;
    }

    return { projects, nextCursor, loading, error, load, loadMore, create };
}

import { computed, ref, shallowRef } from "vue";
import type { ApiError, ApiResult, Schemas } from "../api/client";
import { getMe, getRoles, getUser, listUsers, updateMe, updateUser } from "../api/gea";

type User = Schemas["User"];
/** What an action needs of the caller's role: "prepare", "review" or "administer". */
export type Permission = keyof Schemas["Permissions"];

/**
 * The signed-in user: who they are, their role, and what the role allows.
 *
 * A form asks `can("prepare")` before it offers an action (the workbook's "the current user is
 * not a Viewer"), never `role === "preparer"`: what a role allows is data on the server and can
 * change without a new client. The server refuses the action anyway (403 `forbidden`); this
 * only decides what to show.
 */
export function useCurrentUser() {
    const user = shallowRef<User>();
    const loading = ref(false);
    const error = shallowRef<ApiError>();
    let etag: string | undefined;

    function keep(result: ApiResult<User>): ApiResult<User> {
        loading.value = false;
        error.value = result.error ?? undefined;
        if (result.ok) {
            user.value = result.data;
            etag = result.etag;
        }
        return result;
    }

    /** Load the caller. Call it again after a 403: an Admin may have changed the role in the meantime. */
    async function load(): Promise<ApiResult<User>> {
        loading.value = true;
        return keep(await getMe());
    }

    /** False until the user is loaded, and for a permission the role does not carry. */
    function can(permission: Permission): boolean {
        return user.value?.permissions[permission] ?? false;
    }

    /** True for a user who can only read: the workbook's Viewer. */
    const isReadOnly = computed(() => !can("prepare"));

    async function setHomeRegion(region: string): Promise<ApiResult<User>> {
        if (!etag) {
            const loaded = await load();
            if (!loaded.ok) return loaded;
        }
        loading.value = true;
        return keep(await updateMe(etag as string, { region }));
    }

    return { user, loading, error, load, can, isReadOnly, setHomeRegion };
}

type UserSummary = Schemas["UserSummary"];
type Filters = NonNullable<Parameters<typeof listUsers>[0]>;

/**
 * The people of the application: the list a project owner is chosen from, and, for a user who
 * may administer, giving someone a role or another home region.
 */
export function useUsers() {
    const users = shallowRef<UserSummary[]>([]);
    const roles = shallowRef<Schemas["Role"][]>([]);
    const nextCursor = ref<string | null>(null);
    const loading = ref(false);
    const error = shallowRef<ApiError>();

    let filters: Filters = {};

    async function track<T>(action: () => Promise<ApiResult<T>>): Promise<ApiResult<T>> {
        loading.value = true;
        const result = await action();
        loading.value = false;
        error.value = result.error ?? undefined;
        return result;
    }

    async function load(newFilters: Filters = {}) {
        filters = { ...newFilters, cursor: undefined };
        const result = await track(() => listUsers(filters));
        if (result.ok) {
            users.value = result.data.users;
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    async function loadMore() {
        if (!nextCursor.value) return undefined;
        const result = await track(() => listUsers({ ...filters, cursor: nextCursor.value ?? undefined }));
        if (result.ok) {
            users.value = [...users.value, ...result.data.users];
            nextCursor.value = result.data.nextCursor;
        }
        return result;
    }

    /** The roles to offer, with what each allows. */
    async function loadRoles() {
        const result = await track(() => getRoles());
        if (result.ok) roles.value = result.data.roles;
        return result;
    }

    /**
     * Give a user a role, another home region, or both. Reads the user first, so the change is
     * made against what is stored now; the row in `users` is replaced by the answer.
     */
    async function change(userId: string, changes: Schemas["AdministerUserRequest"]): Promise<ApiResult<User | null>> {
        return track(async () => {
            const current = await getUser(userId);
            if (!current.ok || !current.etag) return current;
            const changed = await updateUser(userId, current.etag, changes);
            if (changed.ok) {
                const { id, name, region, regionName, role, roleName } = changed.data;
                users.value = users.value.map((row) => (row.id === id ? { id, name, region, regionName, role, roleName } : row));
            }
            return changed;
        });
    }

    return { users, roles, nextCursor, loading, error, load, loadMore, loadRoles, change };
}

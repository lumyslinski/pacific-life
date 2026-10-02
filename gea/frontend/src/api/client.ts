import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./generated";

export type Schemas = components["schemas"];
export type Issue = Schemas["Issue"];

/** Codes of the API, plus the two things that can go wrong before an API answer exists. */
export type ErrorCode = Schemas["ErrorCode"] | "network_error" | "unexpected_response";

/**
 * The error half of every result: the `error` object of the server's envelope, plus `retryable`.
 * Whatever went wrong (validation, a stale ETag, a locked run, no network, a 500) arrives in this
 * one shape, so a component needs a single error path: show `message`, mark the fields in
 * `issues`, offer a retry when `retryable`.
 */
export interface ApiError {
    /** Stable, machine-readable reason. Branch on this. */
    code: ErrorCode;
    /** What went wrong, in words for the user. */
    message: string;
    /** HTTP status; 0 when no answer arrived at all. */
    status: number;
    /** One entry per field that is wrong or missing; empty when the error is not about fields. */
    issues: Issue[];
    /** Quote this to support; it identifies the request in the server log. */
    requestId?: string;
    /** True when sending the same request again may succeed (no network, 5xx). */
    retryable: boolean;
}

export interface ApiSuccess<T> {
    ok: true;
    status: number;
    data: T;
    error: null;
    /** Validator of `data`. Send it back in If-Match when changing the resource. */
    etag?: string;
    /** Where a created resource lives. */
    location?: string;
}

export interface ApiFailure {
    ok: false;
    status: number;
    data: null;
    error: ApiError;
}

/**
 * What every function in gea.ts resolves to: the server's envelope `{ data, error }` with the
 * HTTP status, the ETag and an `ok` flag for narrowing. It never rejects:
 *
 *     const result = await createProject(form);
 *     if (!result.ok) { error.value = result.error; return; }
 *     use(result.data);
 */
export type ApiResult<T> = ApiSuccess<T> | ApiFailure;

type TokenProvider = () => string | undefined | Promise<string | undefined>;
let tokenProvider: TokenProvider | undefined;

/** Register how to obtain the bearer token (e.g. from the OIDC library). Call once at app start-up. */
export function setAccessTokenProvider(provider: TokenProvider | undefined): void {
    tokenProvider = provider;
}

const authentication: Middleware = {
    async onRequest({ request }) {
        const token = await tokenProvider?.();
        if (token) request.headers.set("Authorization", `Bearer ${token}`);
        return request;
    },
};

// The only place where the Vue application knows the API origin.
export const api = createClient<paths>({
    baseUrl: import.meta.env.VITE_GEA_API_BASE_URL ?? "http://localhost:8010/gea/v1",
});
api.use(authentication);

/**
 * What openapi-fetch resolves to: the response envelope on success, the error envelope on failure.
 * `error` is optional because openapi-fetch leaves members whose only type is null out of what it returns.
 */
type Raw<T> = Promise<{ data?: { data: T; error?: null }; error?: unknown; response: Response }>;
/** The same for an operation that answers `data: null` (DELETE). */
type RawEmpty = Promise<{ data?: unknown; error?: unknown; response: Response }>;

function serverError(body: unknown): Schemas["ApiError"] | undefined {
    const error = typeof body === "object" && body !== null ? (body as Schemas["ErrorEnvelope"]).error : undefined;
    return typeof error === "object" && error !== null && typeof error.code === "string" && typeof error.message === "string"
        ? error
        : undefined;
}

function errorOf(response: Response, body: unknown): ApiError {
    const requestId = response.headers.get("X-Request-Id") ?? undefined;
    const error = serverError(body);
    if (error) {
        return {
            code: error.code,
            message: error.message,
            status: response.status,
            issues: error.issues ?? [],
            requestId: error.requestId ?? requestId,
            retryable: response.status >= 500,
        };
    }
    // Not from the API itself: a proxy, a gateway, a wrong base URL.
    return {
        code: "unexpected_response",
        message: `The server answered HTTP ${response.status} without an explanation.`,
        status: response.status,
        issues: [],
        requestId,
        retryable: response.status >= 500,
    };
}

/** fetch() rejected: offline, DNS, CORS, a dropped connection. Nothing is known about the outcome. */
function networkError(): ApiError {
    return {
        code: "network_error",
        message: "The server could not be reached. Check the connection and try again.",
        status: 0,
        issues: [],
        retryable: true,
    };
}

function success<T>(response: Response, data: T): ApiSuccess<T> {
    return {
        ok: true,
        status: response.status,
        data,
        error: null,
        etag: response.headers.get("ETag") ?? undefined,
        location: response.headers.get("Location") ?? undefined,
    };
}

function failure(error: ApiError): ApiFailure {
    return { ok: false, status: error.status, data: null, error };
}

/** Unwrap the server's envelope into an ApiResult. */
export async function toResult<T>(request: Raw<T>): Promise<ApiResult<T>> {
    try {
        const { data: envelope, error, response } = await request;
        if (response.ok && envelope !== undefined) return success(response, envelope.data);
        return failure(errorOf(response, error));
    } catch {
        return failure(networkError());
    }
}

/** For an operation whose envelope carries no data (DELETE): `data` is null on success. */
export async function toEmptyResult(request: RawEmpty): Promise<ApiResult<null>> {
    try {
        const { error, response } = await request;
        return response.ok ? success(response, null) : failure(errorOf(response, error));
    } catch {
        return failure(networkError());
    }
}

/** A conditional read (If-None-Match): `data` is null when the resource has not changed (304, no body). */
export async function toConditionalResult<T>(request: Raw<T>): Promise<ApiResult<T | null>> {
    try {
        const { data: envelope, error, response } = await request;
        if (response.status === 304) return success<T | null>(response, null);
        if (response.ok && envelope !== undefined) return success<T | null>(response, envelope.data);
        return failure(errorOf(response, error));
    } catch {
        return failure(networkError());
    }
}

/** Messages per field, for showing next to the inputs: `fieldMessages(error.issues).studyPeriod`. */
export function fieldMessages(issues: readonly Issue[]): Record<string, string[]> {
    const byField: Record<string, string[]> = {};
    for (const issue of issues) {
        if (issue.field) (byField[issue.field] ??= []).push(issue.message);
    }
    return byField;
}

/** Idempotency-Key of one user action. */
export function newIdempotencyKey(): string {
    return globalThis.crypto.randomUUID();
}

/**
 * One Idempotency-Key per user action. The key is reused when the same payload is sent again
 * after a failure that may have been committed on the server (no answer, 5xx), so a retry
 * cannot create a second project, run or contract. It is renewed after a definite answer
 * (success or 4xx) or when the payload changes.
 */
export function createIdempotencyKeeper() {
    let pending: { fingerprint: string; key: string } | undefined;
    return async function withIdempotencyKey<T>(
        payload: unknown,
        action: (key: string) => Promise<ApiResult<T>>,
    ): Promise<ApiResult<T>> {
        const fingerprint = JSON.stringify(payload ?? null);
        if (pending?.fingerprint !== fingerprint) pending = { fingerprint, key: newIdempotencyKey() };
        const result = await action(pending.key);
        if (result.ok || !result.error.retryable) pending = undefined;
        return result;
    };
}

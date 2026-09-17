import createClient from "openapi-fetch";
import type { paths } from "./generated";

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

// The generated paths type is the shared contract. This wrapper is the only
// place where the Vue application knows the API origin.
export const api = createClient<paths>({ baseUrl });

export function newRequestId(): string {
  return globalThis.crypto.randomUUID();
}

export function assertResponse<T>(response: { data?: T; error?: unknown }): T {
  if (response.error !== undefined || response.data === undefined) {
    throw response.error ?? new Error("The calculation API returned no data.");
  }
  return response.data;
}

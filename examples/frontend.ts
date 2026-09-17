// Angular/React can use these HTTP calls. Keep DB credentials in the backend.
type Model = {
  modelId: string; revision: number; immutable: boolean; contentHash: string;
  results: { policyId: string; parameters: Record<string, string> }[];
};
const baseUrl = "http://localhost:8000";
const requestId = () => crypto.randomUUID();

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(baseUrl + path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail ?? `HTTP ${response.status}`);
  return result;
}

export const createCore = () => request<Model>("/core-models", "POST", {
  requestId: requestId(), configId: "CORE_INSURANCE", configRevision: 1,
  policies: [{ policyId: "P-1001", parameters: {
    Death: "250000.00", AccidentalDeath: "300000.00",
    TotalPermanentDisability: "200000.00", CriticalIllness: "150000.00",
  }}],
});

export const createRegionA = (core: Model) => request<Model>("/variations", "POST", {
  requestId: requestId(), coreModelId: core.modelId,
  configId: "REGION_A", configRevision: 1,
});

export const calculate = (variation: Model) => request<Model>(
  `/variations/${variation.modelId}/calculate`, "POST", {
    requestId: requestId(), expectedRevision: variation.revision,
  });

export const editDeath = (variation: Model, amount: string) => request<Model>(
  `/variations/${variation.modelId}`, "PATCH", {
    requestId: requestId(), expectedRevision: variation.revision,
    parameterEdits: { "P-1001": { Death: amount } },
  });

export async function createCustomRegion(core: Model) {
  const functionId = "CUSTOM_CI_" + requestId().replaceAll("-", "").toUpperCase();
  await request(`/functions/${functionId}`, "PUT", {
    expectedRevision: 0,
    definition: {
      arguments: [{ name: "AMOUNT", type: "NUMBER(18,2)" }, { name: "FACTOR", type: "NUMBER(18,8)" }],
      returns: "NUMBER(18,2)", body: "ROUND(AMOUNT * FACTOR, 2)",
    },
  });
  await request(`/functions/${functionId}/preview`, "POST", {
    expectedRevision: 1, arguments: ["125000.00", "0.90"],
  });
  await request(`/functions/${functionId}/publish`, "POST", { expectedRevision: 1 });
  const draft = await request<Model>("/variations", "POST", {
    requestId: requestId(), coreModelId: core.modelId,
    configId: "REGION_A", configRevision: 1, region: "A_CUSTOM",
    bindingOverrides: [{ parameter: "CriticalIllness", functionId, functionRevision: 1,
      arguments: [{ param: "CriticalIllness" }, { constant: "factor" }],
      constants: { factor: "0.90" }, dependsOn: [],
    }],
  });
  return calculate(draft);
}

// Keep each command body/requestId when retrying a transport failure.
// On HTTP 409 reload the model and let the user reconcile the newer revision.
// Editing a draft SQL function never changes an already published release.

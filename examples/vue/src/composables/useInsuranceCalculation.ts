import { ref } from "vue";
import { api, assertResponse, newRequestId } from "../api/client";
import type { components } from "../api/generated";

type CoreModel = components["schemas"]["CoreModel"];
type VariationModel = components["schemas"]["VariationModel"];

export function useInsuranceCalculation() {
  const core = ref<CoreModel>();
  const variation = ref<VariationModel>();
  const loading = ref(false);
  const error = ref<unknown>();

  async function createCore() {
    loading.value = true;
    error.value = undefined;
    try {
      const response = await api.POST("/core-models", {
        body: {
          requestId: newRequestId(),
          configId: "CORE_INSURANCE",
          configRevision: 1,
          policies: [{
            policyId: "P-1001",
            parameters: {
              Death: "250000.00",
              AccidentalDeath: "300000.00",
              TotalPermanentDisability: "200000.00",
              CriticalIllness: "150000.00",
            },
          }],
        },
      });
      core.value = assertResponse(response);
      return core.value;
    } finally {
      loading.value = false;
    }
  }

  async function createRegionalVariation(coreModelId: string, region = "A") {
    const response = await api.POST("/variations", {
      body: {
        requestId: newRequestId(),
        coreModelId,
        configId: "REGION_A",
        configRevision: 1,
        processName: "Regional Calculation",
        region,
      },
    });
    variation.value = assertResponse(response);
    return variation.value;
  }

  async function calculateCurrentVariation() {
    if (!variation.value) throw new Error("Create a variation first.");
    const response = await api.POST("/variations/{modelId}/calculate", {
      params: { path: { modelId: variation.value.modelId } },
      body: {
        requestId: newRequestId(),
        expectedRevision: variation.value.revision,
      },
    });
    variation.value = assertResponse(response);
    return variation.value;
  }

  async function editDeath(value: string) {
    if (!variation.value) throw new Error("Create a variation first.");
    const response = await api.PATCH("/variations/{modelId}", {
      params: { path: { modelId: variation.value.modelId } },
      body: {
        requestId: newRequestId(),
        expectedRevision: variation.value.revision,
        parameterEdits: { "P-1001": { Death: value } },
      },
    });
    variation.value = assertResponse(response);
    return variation.value;
  }

  async function getAudit(modelId: string) {
    const response = await api.GET("/audit/{aggregateId}", {
      params: { path: { aggregateId: modelId }, query: { limit: 100 } },
    });
    return assertResponse(response);
  }

  return { core, variation, loading, error, createCore,
    createRegionalVariation, calculateCurrentVariation, editDeath, getAudit };
}

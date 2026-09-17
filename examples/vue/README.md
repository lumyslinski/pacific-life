# Vue 3 client example

This is a thin browser adapter, not a second calculation engine. Install the
two packages and generate the missing `src/api/generated.ts` from the canonical
OpenAPI document:

```bash
npm install openapi-fetch
npm install --save-dev openapi-typescript
npx openapi-typescript ../../api/openapi.yaml -o src/api/generated.ts
```

Then copy `src/api/client.ts` and
`src/composables/useInsuranceCalculation.ts` into a Vue 3 + TypeScript app.
Set `VITE_API_BASE_URL=http://localhost:8000` for local development. The
composable demonstrates Core Calculation → Regional Calculation → calculation
→ mutable edit → audit retrieval.

This adapter targets the implemented synchronous version 2 API. The revision 3
design will submit `POST /runs`, follow the returned `Location`, poll
`GET /runs/{runId}` with ETag and load the result after `SUCCEEDED`. Keep the
same request ID for transport retries and use the explicit retry endpoint for
failed runs when `retryEligible` is true. Show **Retry calculation**, then
resume status checks for the same run; an edited input starts a new run.
The [retry sequence](../../architecture/12-calculation-retry-sequence.svg)
shows the user action and automatic retry separately.
Contract drafts/publications and frozen run references are
described in [the proposed API](../../api/README.md#proposed-revision-3-contract-and-run-endpoints).
Those routes are not implemented; keep generating this adapter from
`api/openapi.yaml` until the backend and client are migrated together.

The target user journey is **Core first**, producing Silver from Bronze.
After Core succeeds, the user selects a custom process and overrides permitted
input values in a separate scenario. Submit `phase: custom` with its exact
`processName`, for example `Regional Calculation`. The selected DataContract
defines that process's rules. See the [main actor sequence](../../architecture/09-parameter-layer-sequence.svg).

The API uses string decimal values to avoid JavaScript `number` rounding. Keep
those values as strings in Vue forms and display them with a decimal formatter.
Do not calculate insurance amounts in the browser; the database function
release selected by the backend is the source of truth.

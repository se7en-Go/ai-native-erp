import { createClient } from "@hey-api/client-fetch";

// Hand-maintained stand-in for the @hey-api/openapi-ts generated client.
// The backend-owned contracts/openapi.json is not on this branch yet; once
// it lands, `pnpm openapi-ts` regenerates this directory from the contract
// and this file is replaced by the generated output. The shape below mirrors
// the generated client: a fetch client whose base URL the BFF layer sets.
export const client = createClient({
  baseUrl: "/api/bff",
});

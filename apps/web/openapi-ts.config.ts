import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../../contracts/openapi.json",
  output: {
    path: "src/lib/api-client",
    format: "prettier",
  },
  plugins: [
    "@hey-api/typescript",
    "@hey-api/client-fetch",
    {
      name: "@hey-api/sdk",
      asClass: false,
    },
  ],
});

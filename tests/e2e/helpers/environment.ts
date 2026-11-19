const mode = process.env.E2E_MODE ?? "mock";
if (mode !== "mock" && mode !== "live") {
  throw new Error(`Unsupported E2E_MODE=${JSON.stringify(mode)}; use mock or live.`);
}

export const isMockMode = mode === "mock";

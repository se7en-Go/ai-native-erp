// Shared helpers for tests that stub global fetch. The generated hey-api
// SDK calls fetch(Request) while bffFetch calls fetch(url, init); these
// normalize both shapes so handlers and assertions read URL/method/body
// the same way.

/** URL of a fetch mock call, whether made as fetch(url, init) or fetch(Request). */
export function fetchCallUrl(call: unknown[]): string {
  const input = call[0];
  return input instanceof Request ? input.url : String(input);
}

/** HTTP method of a fetch mock call. */
export function fetchCallMethod(call: unknown[]): string | undefined {
  const input = call[0];
  if (input instanceof Request) return input.method;
  return (call[1] as RequestInit | undefined)?.method;
}

/**
 * Request body text of a fetch mock call. Reads the Request stream once;
 * handlers must not read it first (none of these stubs do).
 */
export async function fetchCallBody(call: unknown[]): Promise<string> {
  const input = call[0];
  if (input instanceof Request) return input.text();
  const init = call[1] as RequestInit | undefined;
  return typeof init?.body === "string" ? init.body : "";
}

/** Normalize a stubbed fetch handler's (input, init) into { url, method }. */
export function normalizeFetchInput(
  input: RequestInfo | URL,
  init?: RequestInit,
): { url: string; method: string | undefined } {
  if (input instanceof Request) return { url: input.url, method: input.method };
  return { url: String(input), method: init?.method };
}

import { bffBaseUrl, client } from "@/lib/api-client/client.gen";
import type { ProblemDetail } from "@/lib/contracts";
import { BffApiError } from "@/lib/contracts";

let configured = false;

export function getBffClient() {
  if (!configured) {
    client.setConfig({
      baseUrl: bffBaseUrl(),
      credentials: "include",
    });
    configured = true;
  }
  return client;
}

export async function bffFetch<T>(
  path: string,
  options: RequestInit & { parseJson?: boolean } = {}
): Promise<T> {
  getBffClient();
  const normalizedPath = path.startsWith("/api/bff")
    ? path
    : `/api/bff${path.startsWith("/") ? path : `/${path}`}`;

  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && !headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(normalizedPath, {
    ...options,
    headers,
    credentials: "include",
  });

  if (!res.ok) {
    let problem: ProblemDetail = {
      type: "https://erp.local/errors/http-error",
      title: res.statusText || "Request failed",
      status: res.status,
      detail: `Request failed with status ${res.status}`,
      code: res.status === 403 ? "ERR_INSUFFICIENT_ROLE" : "ERR_API_FAILURE",
    };
    try {
      const parsed = (await res.json()) as Partial<ProblemDetail>;
      problem = { ...problem, ...parsed, status: res.status };
    } catch {
      // keep fallback problem detail
    }
    throw new BffApiError(problem);
  }

  if (options.parseJson === false || res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

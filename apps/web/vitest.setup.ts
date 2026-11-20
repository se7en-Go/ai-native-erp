import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

// `useSearchParams` stays a vi.fn so individual suites can simulate deep links
// such as `/settings/channels?connect=whatsapp`.
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/inbox",
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

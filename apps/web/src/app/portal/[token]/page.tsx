"use client";

import { useParams } from "next/navigation";

import { BuyerPortalPageView } from "@/components/b2b/buyer-portal-view";

/**
 * Dynamic buyer portal route: `/portal/<magic-link-token>`.
 *
 * Next.js route modules may only export the default page plus its segment config,
 * so the presentational surface lives in `@/components/b2b/buyer-portal-view`
 * (which is also rendered directly by the Vitest suite with an explicit token).
 */
export default function BuyerPortalPage() {
  const params = useParams<{ token: string }>();
  const token = typeof params?.token === "string" ? params.token : "";
  return <BuyerPortalPageView token={token} />;
}

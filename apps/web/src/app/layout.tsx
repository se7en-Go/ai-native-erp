import type { Metadata } from "next";
import React from "react";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "Hybrid Omnichannel AI Workbench & ERP",
  description: "Multi-tenant B2C + B2B Cross-Border Omnichannel AI Workbench",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import React from "react";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "工贸全渠道外贸工作台 · Omnichannel ERP",
  description: "多租户 B2B + B2C 跨境电商与工贸一体企业全渠道 AI 工作台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

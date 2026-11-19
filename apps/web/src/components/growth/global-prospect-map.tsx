"use client";

import dynamic from "next/dynamic";
import type { Prospect } from "./types";

export interface ProspectMapTiles {
  url: string;
  /** Trusted tile provider attribution, rendered as text. */
  attribution: string;
  attributionUrl?: string;
  maxZoom?: number;
}

export interface GlobalProspectMapProps {
  prospects: readonly Prospect[];
  /** Omit for local selection, or pass null / an ID to drive selection from a list. */
  selectedId?: string | null;
  onSelect?: (prospectId: string) => void;
  /** Pass null for a tile-free map; otherwise defaults to the OSM standard layer. */
  tiles?: ProspectMapTiles | null;
}

const ClientMap = dynamic(() => import("./leaflet-prospect-map").then((module) => module.LeafletProspectMap), {
  ssr: false,
  loading: () => <div role="status" className="grid h-96 place-items-center rounded-xl bg-slate-100 text-sm text-slate-700">正在加载线索地图…</div>,
});

export function GlobalProspectMap(props: GlobalProspectMapProps) {
  return <ClientMap {...props} />;
}

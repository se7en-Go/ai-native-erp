"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { GlobalProspectMapProps, ProspectMapTiles } from "./global-prospect-map";
import { gradeTone, panel, syncLabel } from "./presentation";
import type { Prospect } from "./types";

const OSM: ProspectMapTiles = {
  url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution: "© OpenStreetMap contributors",
  attributionUrl: "https://www.openstreetmap.org/copyright",
  maxZoom: 19,
};

type LocatedProspect = Prospect & { latitude: number; longitude: number };
function isLocated(prospect: Prospect): prospect is LocatedProspect {
  const { latitude, longitude } = prospect;
  return latitude !== null && longitude !== null && Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
}

/**
 * OSM 的 XYZ 瓦片世界在低缩放下比宽屏视口更窄（zoom 2 = 1024px），
 * Leaflet 会为视口两侧请求 x=-1 / x=2^n 的越界瓦片并得到 400。
 * 因此按容器宽度动态抬高最小缩放，保证世界宽度 >= 视口宽度。
 */
function minZoomForViewport(map: L.Map): number {
  const size = map.getSize();
  const needed = Math.max(size.x, size.y, 256);
  return Math.max(2, Math.ceil(Math.log2(needed / 256)));
}

function popupContent(prospect: LocatedProspect) {
  const content = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = prospect.company_name;
  const location = document.createElement("div");
  location.textContent = [prospect.city, prospect.country].filter(Boolean).join(" · ") || "地区未标注";
  const summary = document.createElement("div");
  summary.textContent = [prospect.grade + " 级", syncLabel[prospect.sync_status], prospect.rating !== null ? "评分 " + prospect.rating : "未评分"].join(" · ");
  content.append(title, location, summary);
  return content;
}

export function LeafletProspectMap({ prospects, selectedId, onSelect, tiles = OSM }: GlobalProspectMapProps) {
  const host = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const markerRefs = useRef(new Map<string, L.Marker>());
  const onSelectRef = useRef(onSelect);
  const [localSelection, setLocalSelection] = useState<string | null>(null);
  const [tileFailure, setTileFailure] = useState<string | null>(null);
  const [visibleTiles, setVisibleTiles] = useState<string | null>(null);
  const located = useMemo(() => prospects.filter(isLocated), [prospects]);
  const activeId = selectedId === undefined ? localSelection : selectedId;
  const tileUrl = tiles?.url;
  const maxZoom = tiles?.maxZoom ?? 19;

  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);

  useEffect(() => {
    if (!host.current) return;
    // 宽屏低缩放下 OSM 会请求 x/y 越界的瓦片（1/-1/0.png、1/2/0.png）导致 400。
    // 限制最小缩放、世界边界并关闭横向循环，配合 noWrap 彻底根除越界请求。
    const map = L.map(host.current, {
      minZoom: 2,
      maxZoom: 19,
      attributionControl: false,
      scrollWheelZoom: false,
      zoomAnimation: false,
      fadeAnimation: false,
      worldCopyJump: false,
      maxBounds: [[-85, -180], [85, 180]],
      maxBoundsViscosity: 1.0,
    });
    const initialMinZoom = minZoomForViewport(map);
    map.setMinZoom(initialMinZoom);
    map.setView([18, 0], initialMinZoom);
    mapRef.current = map;
    const observer =
      typeof ResizeObserver === "undefined"
        ? undefined
        : new ResizeObserver(() => {
            map.invalidateSize({ pan: false });
            const nextMinZoom = minZoomForViewport(map);
            map.setMinZoom(nextMinZoom);
            if (map.getZoom() < nextMinZoom) map.setZoom(nextMinZoom);
          });
    observer?.observe(host.current);
    return () => {
      observer?.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !tileUrl) return;
    let failed = false;
    const layer = L.tileLayer(tileUrl, {
      maxNativeZoom: maxZoom,
      minZoom: 2,
      noWrap: true,
      bounds: [[-85, -180], [85, 180]],
    })
      .on("loading", () => { failed = false; })
      .on("tileload", () => setVisibleTiles(tileUrl))
      .on("tileerror", () => { failed = true; setTileFailure(tileUrl); })
      .on("load", () => { if (!failed) setTileFailure(null); })
      .addTo(map);
    return () => { layer.off(); layer.remove(); };
  }, [tileUrl, maxZoom]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const layer = L.layerGroup().addTo(map);
    const markers = markerRefs.current;
    for (const prospect of located) {
      const marker = L.marker([prospect.latitude, prospect.longitude], {
        icon: L.divIcon({
          className: "grid place-items-center rounded-full border-2 border-white shadow-md text-xs font-bold " + gradeTone[prospect.grade],
          iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -13],
        }),
      }).bindPopup(() => popupContent(prospect)).on("click", () => {
        setLocalSelection(prospect.id);
        onSelectRef.current?.(prospect.id);
      }).addTo(layer);
      const el = marker.getElement();
      if (el) {
        el.textContent = prospect.grade;
        el.setAttribute("aria-label", prospect.company_name);
        L.DomEvent.on(el, "keydown", (event) => {
          if (event instanceof KeyboardEvent && event.key === " ") { event.preventDefault(); el.click(); }
        });
      }
      markers.set(prospect.id, marker);
    }
    if (located.length) {
      map.fitBounds(located.map(({ latitude, longitude }) => [latitude, longitude]), { animate: false, padding: [32, 32], maxZoom: 10 });
    }
    return () => {
      for (const marker of markers.values()) {
        const el = marker.getElement();
        if (el) L.DomEvent.off(el);
      }
      markers.clear();
      layer.clearLayers();
      layer.remove();
    };
  }, [located]);

  useEffect(() => {
    for (const [id, marker] of markerRefs.current) {
      const active = id === activeId;
      const el = marker.getElement();
      el?.setAttribute("aria-pressed", String(active));
      el?.classList.toggle("ring-2", active);
      el?.classList.toggle("ring-blue-700", active);
      marker.setZIndexOffset(active ? 1000 : 0);
    }
  }, [activeId, located]);

  return (
    <section aria-label="全球线索地图" className={`${panel} self-start`}>
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-bold">全球线索地图</h2>
        <span className="rounded-full bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-900">{located.length} 条已定位</span>
      </header>
      <div className="relative isolate overflow-hidden rounded-xl border border-slate-200">
        <div ref={host} className="relative z-0 h-96 w-full !bg-slate-100 [background-image:radial-gradient(#94a3b8_1px,transparent_1px)] [background-size:18px_18px]" />
        {(!tileUrl || tileFailure === tileUrl) && <p role="status" className="pointer-events-none absolute right-2 top-2 rounded bg-white/95 px-2 py-1 text-xs text-slate-700 shadow-sm">底图不可用 · 仍可查看线索标记</p>}
        {tiles && visibleTiles === tileUrl && <p className="absolute bottom-0 right-0 rounded-tl bg-white/95 px-2 py-0.5 text-xs text-slate-700">
          {tiles.attributionUrl ? <a href={tiles.attributionUrl} className="underline">{tiles.attribution}</a> : tiles.attribution}
        </p>}
      </div>
      {!located.length && <p className="mt-2 text-sm text-slate-600">暂无具有有效坐标的线索。</p>}
    </section>
  );
}

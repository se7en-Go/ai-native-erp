const DAY_MS = 86_400_000;

export function parseKey(key: string): Date {
  const [year, month, day] = key.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

export function keyOf(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function addDays(key: string, days: number): string {
  return keyOf(new Date(parseKey(key).getTime() + days * DAY_MS));
}

/** ISO datetime -> UTC calendar day; date-only input is returned unchanged. */
export function isoToKey(value: string | null): string | null {
  return value ? value.slice(0, 10) : null;
}

/** Monday-first week start for a calendar day key. */
export function startOfWeek(key: string): string {
  const date = parseKey(key);
  return addDays(key, -((date.getUTCDay() + 6) % 7));
}

export function monthLabel(key: string): string {
  const date = parseKey(key);
  return date.getUTCFullYear() + " 年 " + (date.getUTCMonth() + 1) + " 月";
}

export function monthMatrix(key: string): { key: string; inMonth: boolean }[] {
  const date = parseKey(key);
  const first = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
  const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0));
  const start = startOfWeek(keyOf(first));
  const end = addDays(startOfWeek(keyOf(last)), 6);
  const cells: { key: string; inMonth: boolean }[] = [];
  for (let cursor = start; cursor <= end; cursor = addDays(cursor, 1)) {
    cells.push({ key: cursor, inMonth: parseKey(cursor).getUTCMonth() === date.getUTCMonth() });
  }
  return cells;
}

export function weekKeys(key: string): string[] {
  const start = startOfWeek(key);
  return Array.from({ length: 7 }, (_, index) => addDays(start, index));
}

export function formatTime(value: string, timeZone: string): string {
  if (!value.includes("T")) return "";
  return new Intl.DateTimeFormat("zh-CN", { timeZone, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

export const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"] as const;

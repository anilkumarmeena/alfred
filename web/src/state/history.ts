// History view state: paged day list, the open day's exchanges, and search. Newest first. Open day = panel `item`.
import { signal } from "@preact/signals";
import type { DayDetail, Days, Found } from "../lib/history";
import { act, item, section } from "./panel";

export const days = signal<Days | null>(null);
export const olderBusy = signal(false);
export const day = signal<DayDetail | null>(null);
export const found = signal<Found | null>(null);  // search hits; replace the day list while set
export const searching = signal(false);

// Refresh the first page of days, keeping older pages already loaded so the reader keeps their place.
export async function loadDays() {
  const r = await act<Days>("conversations");
  if (!r) return;
  const have = days.peek(), oldest = r.days.at(-1)?.date;
  const kept = have && r.more && oldest ? have.days.filter((d) => d.date < oldest) : [];
  days.value = kept.length ? { ...r, days: [...r.days, ...kept], more: have!.more } : r;
}

// Load the next page of older days.
export async function loadOlderDays() {
  const have = days.peek();
  if (!have?.more || olderBusy.peek()) return;
  olderBusy.value = true;
  const r = await act<Days>("conversations", { before: have.days.at(-1)?.date ?? "" });
  olderBusy.value = false;
  if (r) days.value = { ...r, days: [...have.days, ...r.days] };
}

// Load a day's exchanges; dropped if another day was opened meanwhile.
export async function loadDay(date: string) {
  const r = await act<DayDetail>("conversation", { date });
  if (r && item.peek() === date && section.peek() === "history") day.value = r;
}

export async function search(text: string): Promise<boolean> {
  searching.value = true;
  const r = await act<Found>("conversation_search", { text });
  searching.value = false;
  if (r) found.value = r;
  return !!r;
}

export function clearSearch() { found.value = null; }

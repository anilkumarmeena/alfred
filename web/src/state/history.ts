// The History view's data: the days with conversations, a page at a time, the open day's exchanges, and a search over
// all of them, from jarvis.panel (jarvis.state.history). Newest first throughout. The open day is the panel's `item`.
import { signal } from "@preact/signals";
import type { DayDetail, Days, Found } from "../lib/history";
import { act, item, section } from "./panel";

export const days = signal<Days | null>(null);
export const olderBusy = signal(false);
export const day = signal<DayDetail | null>(null);
export const found = signal<Found | null>(null);  // the search's hits, shown in place of the days while there's one
export const searching = signal(false);

// The first page of days, afresh (when History opens, or the page comes back on screen), keeping any older pages
// already shown, so a refresh doesn't lose the reader's place.
export async function loadDays() {
  const r = await act<Days>("conversations");
  if (!r) return;
  const have = days.peek(), oldest = r.days.at(-1)?.date;
  const kept = have && r.more && oldest ? have.days.filter((d) => d.date < oldest) : [];
  days.value = kept.length ? { ...r, days: [...r.days, ...kept], more: have!.more } : r;
}

// The next page of older days, after those listed.
export async function loadOlderDays() {
  const have = days.peek();
  if (!have?.more || olderBusy.peek()) return;
  olderBusy.value = true;
  const r = await act<Days>("conversations", { before: have.days.at(-1)?.date ?? "" });
  olderBusy.value = false;
  if (r) days.value = { ...r, days: [...have.days, ...r.days] };
}

// One day's exchanges, kept only if that day is still the one open.
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

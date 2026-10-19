// Past-conversation types (from the conversations / conversation / conversation_search panel actions) and their
// formatting. Newest first throughout.

// A logged exchange: speaker, words, commands they approved, and Alfred's reply. Between-turn speech (e.g. a task
// update) is an announcement; an unrecognised line is a note.
export interface Entry {
  n: number; time: string; kind: "exchange" | "announcement" | "note"; said: string;
  who?: string; approved?: string[]; reply?: string; interrupted?: boolean;
}
export interface Day {
  date: string; count: number; exchanges: number; first: string; last: string; preview: string;
}
export interface Days { days: Day[]; more: boolean; today: string }
export interface DayDetail { date: string; entries: Entry[]; newer: string | null; older: string | null }
export interface Hit extends Entry { date: string }
export interface Found { text: string; hits: Hit[]; more: boolean }

const DAY_MS = 86_400_000;
const utc = (date: string) => new Date(`${date}T00:00:00Z`);

// "Today", "Yesterday" or "Monday 5 October" (year added if not current). `today` is Alfred's computer's date.
export function dayLabel(date: string, today: string): string {
  if (date === today) return "Today";
  const d = utc(date);
  if (today && utc(today).getTime() - d.getTime() === DAY_MS) return "Yesterday";
  const sameYear = today ? today.slice(0, 4) === date.slice(0, 4) : true;
  return d.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", ...(sameYear ? {} : { year: "numeric" }), timeZone: "UTC",
  }).replace(",", "");
}

// "09:14–21:53", or a single time when equal
export const span = (first: string, last: string) => (first && last && first !== last ? `${first}–${last}` : first || last);

// "12 exchanges", "1 exchange · 3 updates"
export function tally(exchanges: number, count: number): string {
  const others = count - exchanges;
  const said = `${exchanges} exchange${exchanges === 1 ? "" : "s"}`;
  return others > 0 ? `${said} · ${others} update${others === 1 ? "" : "s"}` : said;
}

// Split `text` into runs flagged when they match one of `words` (case-insensitive), for hit highlighting.
export function marks(text: string, words: string[]): { text: string; hit: boolean }[] {
  const wanted = words.map((w) => w.trim()).filter(Boolean);
  if (!wanted.length || !text) return text ? [{ text, hit: false }] : [];
  const escaped = wanted.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const out: { text: string; hit: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(new RegExp(escaped.join("|"), "gi"))) {
    if (m.index! > last) out.push({ text: text.slice(last, m.index), hit: false });
    out.push({ text: m[0], hit: true });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out;
}

// Group search hits by day, preserving order (newest first).
export function byDay(hits: Hit[]): { date: string; hits: Hit[] }[] {
  const out: { date: string; hits: Hit[] }[] = [];
  for (const h of hits) {
    if (out.at(-1)?.date === h.date) out.at(-1)!.hits.push(h);
    else out.push({ date: h.date, hits: [h] });
  }
  return out;
}

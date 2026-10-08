// Past conversations, as jarvis.panel's conversations, conversation and conversation_search actions return them
// (jarvis.state.history), and how the History view words them. Newest first throughout: the days, and the exchanges
// within a day.

// One logged exchange: who spoke, what they said, the commands their words said yes to and JARVIS's reply. Something
// JARVIS said between turns (a task update, say) is an announcement; a line in no known shape is a note.
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

// "Today", "Yesterday", or "Monday 5 October" (with the year when it isn't this one), `today` being the Mac's date.
export function dayLabel(date: string, today: string): string {
  if (date === today) return "Today";
  const d = utc(date);
  if (today && utc(today).getTime() - d.getTime() === DAY_MS) return "Yesterday";
  const sameYear = today ? today.slice(0, 4) === date.slice(0, 4) : true;
  return d.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", ...(sameYear ? {} : { year: "numeric" }), timeZone: "UTC",
  }).replace(",", "");
}

// "09:14–21:53", or the one time when they're the same
export const span = (first: string, last: string) => (first && last && first !== last ? `${first}–${last}` : first || last);

// "12 exchanges", "1 exchange · 3 updates"
export function tally(exchanges: number, count: number): string {
  const others = count - exchanges;
  const said = `${exchanges} exchange${exchanges === 1 ? "" : "s"}`;
  return others > 0 ? `${said} · ${others} update${others === 1 ? "" : "s"}` : said;
}

// `text` cut into runs, each marked whether it's one of `words` (in any case), for a search's hits to show them.
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

// A search's hits in runs by day, keeping their order (newest first), for a heading over each day.
export function byDay(hits: Hit[]): { date: string; hits: Hit[] }[] {
  const out: { date: string; hits: Hit[] }[] = [];
  for (const h of hits) {
    if (out.at(-1)?.date === h.date) out.at(-1)!.hits.push(h);
    else out.push({ date: h.date, hits: [h] });
  }
  return out;
}

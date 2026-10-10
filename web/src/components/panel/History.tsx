// History: day list (newest first), a day's exchanges with prev/next day navigation, and full-text search.
import { useEffect, useRef } from "preact/hooks";
import { byDay, dayLabel, marks, span, tally, type Day, type Entry } from "../../lib/history";
import { clearSearch, day, days, found, loadDay, loadOlderDays, olderBusy, search, searching } from "../../state/history";
import { go, item, openTask, roomy } from "../../state/panel";
import { BackIcon, ChevronIcon, SearchIcon } from "../icons";
import { Empty, InlineForm } from "./bits";

const TASK = /\b([Tt]ask) (\d+)\b/g;
const today = () => days.value?.today ?? "";
const openDay = (date: string) => go("history", date);

// Text with search hits marked and each "task N" as a button.
function Said({ text, words = [] }: { text: string; words?: string[] }) {
  const parts: preact.ComponentChildren[] = [];
  let last = 0;
  const plain = (t: string) => marks(t, words).forEach((m) => parts.push(m.hit ? <mark>{m.text}</mark> : m.text));
  for (const m of text.matchAll(TASK)) {
    plain(text.slice(last, m.index));
    parts.push(<button class="tref" onClick={() => openTask(Number(m[2]))} title={`Open task ${m[2]}`}>{m[0]}</button>);
    last = m.index! + m[0].length;
  }
  plain(text.slice(last));
  return <>{parts}</>;
}

// One exchange (user words, approved commands, reply), or a between-turn announcement.
export function Exchange({ e, words }: { e: Entry; words?: string[] }) {
  const said = <p><Said text={e.said} words={words} /></p>;
  return (
    <div class="hx">
      <div class="hx-time">{e.time}</div>
      {e.kind === "exchange" ? <>
        <div class="hx-msg you"><span class="who">{e.who}</span>{said}</div>
        {e.approved?.map((c, i) => <div key={i} class="hx-yes"><span class="who">Approved</span><code>{c}</code></div>)}
        <div class="hx-msg jarvis">
          <span class="who">JARVIS</span>
          {e.reply ? <p><Said text={e.reply} words={words} />{e.interrupted && <span class="cut"> · cut off</span>}</p>
            : <p class="cut">{e.interrupted ? "Interrupted before replying" : "No reply"}</p>}
        </div>
      </> : e.kind === "announcement"
        ? <div class="hx-msg jarvis ann"><span class="who">JARVIS · update</span>{said}</div>
        : <div class="hx-msg note">{said}</div>}
    </div>
  );
}

function DayRow({ d }: { d: Day }) {
  const on = item.value === d.date;
  return (
    <button class={`task hday${on ? " sel" : ""}`} aria-current={on ? "true" : undefined} onClick={() => openDay(d.date)}>
      <div class="t1"><span class="title">{dayLabel(d.date, today())}</span><span class="id">{span(d.first, d.last)}</span><ChevronIcon /></div>
      <div class="meta">{tally(d.exchanges, d.count)}</div>
      {d.preview && <div class="hprev">{d.preview}</div>}
    </button>
  );
}

function Results() {
  const f = found.value!, words = f.text.split(" ");
  return (
    <>
      <div class="grp">{f.hits.length}{f.more ? "+" : ""} match{f.hits.length === 1 ? "" : "es"} for “{f.text}”
        <button class="act" onClick={clearSearch}>Clear</button>
      </div>
      {!f.hits.length && <Empty>Nothing said mentions that. Try fewer or other words.</Empty>}
      {byDay(f.hits).map((g) => (
        <section key={g.date} class="hgroup">
          <button class="linkish hdate" onClick={() => openDay(g.date)}>{dayLabel(g.date, today())}<ChevronIcon /></button>
          {g.hits.map((e) => <Exchange key={e.n} e={e} words={words} />)}
        </section>
      ))}
      {f.more && <p class="hint">Only the latest {f.hits.length} are shown; add a word to narrow it.</p>}
    </>
  );
}

export function DayList() {
  const all = days.value;
  return (
    <>
      <div class="hsearch">
        <InlineForm placeholder="Search past conversations" label="Search" send={search}>
          {searching.value ? "…" : <SearchIcon />}
        </InlineForm>
      </div>
      {found.value ? <Results /> : !all ? <Empty>Loading…</Empty> : <>
        {all.days.map((d) => <DayRow key={d.date} d={d} />)}
        {!all.days.length && <Empty>No conversations yet. Each one is kept here, a day at a time, from the first.</Empty>}
        {all.more && <button class="btn sm hmore" disabled={olderBusy.value} onClick={loadOlderDays}>
          {olderBusy.value ? "Loading…" : "Older days"}</button>}
      </>}
    </>
  );
}

// Adjacent-day navigation
function Steps({ newer, older }: { newer: string | null; older: string | null }) {
  return (
    <div class="hsteps">
      <button class="btn sm" disabled={!newer} onClick={() => newer && openDay(newer)}><BackIcon />{newer ? dayLabel(newer, today()) : "Newer"}</button>
      <button class="btn sm" disabled={!older} onClick={() => older && openDay(older)}>{older ? dayLabel(older, today()) : "Older"}<ChevronIcon /></button>
    </div>
  );
}

export function DayView({ date }: { date: string }) {
  const top = useRef<HTMLDivElement>(null);
  const d = day.value?.date === date ? day.value : null;
  useEffect(() => {
    day.value = null;
    loadDay(date);
    top.current?.closest(".col, .pbody")?.scrollTo?.({ top: 0 });
  }, [date]);
  const entries = d?.entries ?? [], exchanges = entries.filter((e) => e.kind === "exchange").length;
  const label = dayLabel(date, today()), full = dayLabel(date, "");  // relative label plus full date
  return (
    <div ref={top} class="hday-view">
      <p class="big">{label}</p>
      {d && <p class="sub hsum">{label !== full && `${full} · `}{entries.length ? `${tally(exchanges, entries.length)} · ${span(entries.at(-1)!.time, entries[0]!.time)} · newest first` : "Nothing was said this day."}</p>}
      {d && <Steps newer={d.newer} older={d.older} />}
      {!d ? <Empty>Loading…</Empty> : entries.map((e) => <Exchange key={e.n} e={e} />)}
      {d?.older && entries.length > 3 && <button class="task hnext" onClick={() => openDay(d.older!)}>
        <div class="t1"><span class="title">Keep going back: {dayLabel(d.older, today())}</span><ChevronIcon /></div>
      </button>}
    </div>
  );
}

export function HistorySection() {
  const open = item.value, view = open ? <DayView date={open} /> : null;
  if (roomy.value) {
    return (
      <div class="split">
        <div class="col lead"><DayList /></div>
        <div class="col">{view ?? <Empty>Pick a day to read what was said, or search for something.</Empty>}</div>
      </div>
    );
  }
  return view ?? <DayList />;
}

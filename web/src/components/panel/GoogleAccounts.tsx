// Google accounts: set up the OAuth client, add accounts, sign them in (and again), remove them.
// A sign-in opens Google in a new tab; Google comes back to JARVIS's web service (Desktop app client, on its computer)
// or to this page's own address (Web application client, any device). On a phone with a Desktop app client, Google's
// last page doesn't load: its address is pasted here instead. The page follows the sign-in until it's over.
import { useEffect } from "preact/hooks";
import { useSignal, type Signal } from "@preact/signals";
import { accounts, act, googleSetup, loadGoogleSetup, toast, type Account, type ApiCheck, type GoogleSetup, type SignIn, type SignInResult } from "../../state/panel";
import { EditIcon, TrashIcon } from "../icons";
import { Empty, InlineForm, Led, Pill } from "./bits";

const CONSOLE = "https://console.cloud.google.com";
const RETRY_MS = 3000;

// "A, B and C" as links to turning each API on
const links = (list: GoogleSetup["apis"]) => list.map((a, i) => (
  <span key={a.name}>{i ? (i === list.length - 1 ? " and " : ", ") : ""}<a href={a.url} target="_blank" rel="noopener">{a.name}</a></span>
));

function ClientPaste() {
  const value = useSignal(""), busy = useSignal(false);
  async function save(e: Event) {
    e.preventDefault();
    if (!value.value.trim() || busy.value) return;
    busy.value = true;
    const r = await act("key_set", { name: "google.client", value: value.value }, "Kept, encrypted");
    busy.value = false;
    if (r) { value.value = ""; await loadGoogleSetup(); }
  }
  return (
    <form class="gsetup-paste" onSubmit={save}>
      <textarea class="field mono" rows={3} autocomplete="off" spellcheck={false} placeholder='{"installed": {"client_id": …}} or {"web": …}'
        aria-label="OAuth client JSON" value={value.value} onInput={(e) => { value.value = e.currentTarget.value; }} />
      <button class="btn sm primary" disabled={busy.value || !value.value.trim()}>Save the client</button>
    </form>
  );
}

function Redirects({ list }: { list: string[] }) {
  return <ul class="gsetup-uris">{list.map((u) => <li key={u}><code>{u}</code></li>)}</ul>;
}

// No client yet: how to make one in Google Cloud, then paste its JSON.
function SetupCard({ setup }: { setup: GoogleSetup }) {
  return (
    <div class="srv gsetup">
      <h3>Connect JARVIS to Google</h3>
      <p class="sub">Once per JARVIS: make an OAuth client in your own Google Cloud project. About five minutes.</p>
      <ol class="steps">
        <li>Open the <a href={`${CONSOLE}/auth/branding`} target="_blank" rel="noopener">Google Auth Platform</a> in a project of yours
          (or a new one), set it up as <b>External</b>, and add yourself under <a href={`${CONSOLE}/auth/audience`} target="_blank" rel="noopener">Audience → Test users</a>.</li>
        <li>Enable the APIs: {links(setup.apis.filter((a) => !a.chat))}. For Google Chat, also {links(setup.apis.filter((a) => a.chat))}.</li>
        <li><a href={`${CONSOLE}/auth/clients/create`} target="_blank" rel="noopener">Create a client</a>, of either type:
          <ul class="gsetup-kinds">
            <li><b>Desktop app</b>: simplest. Signing in from a phone ends with pasting one address.</li>
            <li><b>Web application</b>: signs in from any device by itself. Add these as <b>Authorised redirect URIs</b>:
              <Redirects list={setup.redirects} /></li>
          </ul>
        </li>
        <li>Download the client's JSON, then paste the whole file here:</li>
      </ol>
      <ClientPaste />
      <p class="hint">While the app is in Testing, Google ends sign-ins after 7 days: publish it (Audience → Publish app) to keep them.</p>
    </div>
  );
}

function ClientLine({ setup }: { setup: GoogleSetup }) {
  const replacing = useSignal(false);
  const how = setup.kind === "web" ? "Web application: signs in from any device by itself"
    : setup.paste ? "Desktop app: from this device, a sign-in ends with pasting one address"
      : "Desktop app: signs in on this computer by itself";
  return (
    <>
      <div class="gclient sub">
        <span>OAuth client · {how}</span>
        <button class="linkish" onClick={() => { replacing.value = !replacing.value; }}>{replacing.value ? "Keep it" : "Replace"}</button>
      </div>
      {setup.missing.length > 0 && (
        <div class="note amber">
          <div>Add {setup.missing.length > 1 ? "these" : "this"} to the client's Authorised redirect URIs in
            {" "}<a href={`${CONSOLE}/auth/clients`} target="_blank" rel="noopener">Google Cloud</a>, then paste its JSON again
            (Replace): <Redirects list={setup.missing} /></div>
        </div>
      )}
      {replacing.value && <ClientPaste />}
    </>
  );
}

// Open a tab during the click (browsers block tabs opened later); Google's URL goes into it once the server answers.
export function blankTab(): Window | null {
  try {
    const w = window.open("about:blank", "_blank");
    if (w) w.opener = null;
    return w;
  } catch { return null; }
}

// One account's sign-in or check: `started` once Google's URL is back (a check has none), `result` once it's over
interface Flow { label: string; step: number; started?: SignIn; result?: SignInResult }

// Follow the started sign-in until it's over: the server answers once Google comes back, or "waiting" after a while.
function useSignInFlow(flow: Signal<Flow | null>) {
  const state = flow.value?.result ? null : flow.value?.started?.state;
  useEffect(() => {
    if (!state) return;
    let live = true;
    (async () => {
      while (live) {
        const r = await act<SignInResult>("google_sign_in_status", { state });
        if (!live || flow.peek()?.started?.state !== state) return;
        if (!r) await new Promise((done) => setTimeout(done, RETRY_MS));  // offline or a server error: ask again soon
        else if (r.state !== "waiting") return finished(flow, r);
      }
    })();
    return () => { live = false; };
  }, [state]);
}

const toFix = (r: SignInResult) => (r.checks ?? []).filter((c) => c.state === "off" || c.state === "error");

// Signed in with everything working: say so and close. Anything to fix (an API turned off): keep the box open.
function finished(flow: Signal<Flow | null>, r: SignInResult) {
  if (r.accounts) accounts.value = r.accounts;
  const f = flow.peek();
  if (r.state === "done" && !toFix(r).length && !r.notes?.length) { toast(r.said ?? "Signed in"); flow.value = null; return; }
  if (f) flow.value = { ...f, result: r };
}

// Check which Google APIs work for a signed-in account; false if the check couldn't run
async function check(flow: Signal<Flow | null>, label: string): Promise<boolean> {
  const r = await act<SignInResult>("google_check", { label });
  if (r && flow.peek()?.label === label) finished(flow, r);
  return !!r;
}

const LED: Record<ApiCheck["state"], ["ok" | "warn" | "bad" | "off", string]> = {
  ok: ["ok", "Ready"], off: ["warn", "Turned off"], error: ["bad", "Didn't answer"], denied: ["off", "Not allowed"],
};

function Checks({ checks }: { checks: ApiCheck[] }) {
  return (
    <ul class="list">{checks.map((c) => (
      <li key={c.name} class="row">
        <Led kind={LED[c.state][0]} /><div class="grow">{c.name}{c.why && <div class="sub">{c.why}</div>}</div>
        {c.state === "off" ? <a class="btn sm gon" href={c.fix} target="_blank" rel="noopener">Turn on</a> : <span class="sub">{LED[c.state][1]}</span>}
      </li>
    ))}</ul>
  );
}

function SignedIn({ flow, r }: { flow: Signal<Flow | null>; r: SignInResult }) {
  const busy = useSignal(false);
  async function again() {
    busy.value = true;
    await check(flow, flow.peek()!.label);
    busy.value = false;
  }
  const fixes = toFix(r);
  return (
    <div class={`note gsign${fixes.length ? " amber" : ""}`}>
      <div><b>{fixes.length ? "Signed in, almost ready" : "Signed in"}</b>{r.email && <span class="sub"> · {r.email}</span>}</div>
      {r.checks && <Checks checks={r.checks} />}
      {fixes.some((c) => c.state === "off") && <div class="sub">Turn each one on in Google Cloud, give it a minute, then check again. No need to sign in again.</div>}
      {r.notes?.map((n) => <div key={n} class="sub">{n}</div>)}
      <div class="inline wrap">
        {fixes.length > 0 && <button class="btn sm primary" disabled={busy.value} onClick={again}>{busy.value ? "Checking…" : "Check again"}</button>}
        <button class="btn sm" onClick={() => { flow.value = null; }}>{fixes.length ? "Later" : "Done"}</button>
      </div>
    </div>
  );
}

// `tab` is opened by the caller during its click when it awaits something first
async function startSignIn(flow: Signal<Flow | null>, label: string, step: number, tab: Window | null = blankTab()) {
  flow.value = { label, step };
  const r = await act<SignIn>("google_sign_in", { label, step, origin: location.origin });
  if (!r) { tab?.close(); flow.value = null; return; }
  if (tab && !tab.closed) tab.location.href = r.url;
  flow.value = { label, step, started: r };
}

// How it went arrives through the status request already waiting on the sign-in (useSignInFlow)
function PasteForm() {
  const address = useSignal(""), busy = useSignal(false);
  async function paste(e: Event) {
    e.preventDefault();
    if (!address.value.trim() || busy.value) return;
    busy.value = true;
    await act("google_sign_in_paste", { address: address.value });
    busy.value = false;
  }
  return (
    <form class="inline wrap gpaste" onSubmit={paste}>
      <div class="sub">At the end, Google opens a page on <code>127.0.0.1</code> that doesn't load. That's expected: copy its whole address and paste it here.</div>
      <input class="field mono" type="url" autocomplete="off" placeholder="http://127.0.0.1:8787/api/google/callback?state=…"
        aria-label="Address of the page that didn't load" value={address.value} onInput={(e) => { address.value = e.currentTarget.value; }} />
      <button class="btn sm primary" disabled={busy.value || !address.value.trim()}>Finish</button>
    </form>
  );
}

function SignInBox({ flow }: { flow: Signal<Flow | null> }) {
  const f = flow.value!;
  useSignInFlow(flow);
  const { started, result } = f;
  if (result?.state === "done") return <SignedIn flow={flow} r={result} />;  // also a check's, which has no `started`
  if (!started) return <div class="note gsign">Working…</div>;
  // A refusal offers the next, narrower scope step when there is one (Chat accounts), else the same again
  const again = (bump: 0 | 1) => startSignIn(flow, f.label, f.step + bump);
  if (result) {
    const narrower = result.state === "refused" && started.next;
    return (
      <div class="note amber gsign">
        <div class="grow">{result.said}</div>
        <div class="inline wrap">
          {result.state !== "old" && <button class={`btn sm${narrower ? " primary" : ""}`} onClick={() => again(narrower ? 1 : 0)}>
            Try again{narrower ? ` ${started.next}` : ""}</button>}
          <button class="btn sm" onClick={() => { flow.value = null; }}>Close</button>
        </div>
      </div>
    );
  }
  return (
    <div class="note gsign">
      <div class="grow">
        Finish in the Google tab: pick the <b>{f.label}</b> account and allow every permission it lists.
        {" "}<a href={started.url} target="_blank" rel="noopener">Open Google again</a> if no tab opened.
      </div>
      {started.next && (
        <div class="sub">If Google says <b>Access blocked</b>, the Workspace blocks Google Chat for JARVIS: close that tab and
          {" "}<button class="linkish" onClick={() => again(1)}>try again {started.next}</button></div>
      )}
      {started.paste && <PasteForm />}
      <div class="inline"><span class="sub grow">Waiting for Google…</span><button class="btn sm" onClick={() => { flow.value = null; }}>Cancel</button></div>
    </div>
  );
}

function AccountRow({ a, flow, ready }: { a: Account; flow: Signal<Flow | null>; ready: boolean }) {
  const editing = useSignal(false);
  const signing = flow.value?.label === a.label;
  async function remove() {
    if (!confirm(`Remove the ${a.label} account? JARVIS forgets its sign-in; your Google account isn't touched.`)) return;
    const r = await act<Account[]>("account_remove", { label: a.label }, `Removed the ${a.label} account`);
    if (r) { accounts.value = r; if (signing) flow.value = null; }
  }
  return (
    <li class="row gacct">
      <div class="grow">
        <div><b>{a.label}</b> {a.default && <Pill kind="running">default</Pill>} {a.chat && <Pill kind="muted">Chat</Pill>}</div>
        <div class="sub">{a.email || "email known once signed in"}{a.note ? ` · ${a.note}` : ""}</div>
        {!a.signed_in && !signing && <div class="sub warn">Not signed in yet</div>}
        {editing.value && <InlineForm placeholder="What it's for" label="Save" initial={a.note} send={async (note) => {
          const r = await act<Account[]>("account_note", { label: a.label, note }, "Saved");
          if (r) { accounts.value = r; editing.value = false; }
          return !!r;
        }} />}
        {signing && <SignInBox flow={flow} />}
      </div>
      <span class="rt">
        {!signing && ready && <button class={`btn sm${a.signed_in ? "" : " primary"}`} onClick={() => startSignIn(flow, a.label, 0)}>
          {a.signed_in ? "Sign in again" : "Sign in"}</button>}
        {!signing && a.signed_in && <button class="btn sm" title="Check which Google services work" onClick={async () => {
          flow.value = { label: a.label, step: 0 };
          if (!await check(flow, a.label)) flow.value = null;
        }}>Check</button>}
        {!a.default && <button class="btn sm" onClick={async () => { const r = await act<Account[]>("account_default", { label: a.label }, `${a.label} is the default`); if (r) accounts.value = r; }}>Make default</button>}
        <button class="ico" title="Edit what it's for" aria-label="Edit note" onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>
        <button class="ico" title="Remove" aria-label={`Remove ${a.label}`} onClick={remove}><TrashIcon /></button>
      </span>
    </li>
  );
}

function AddAccount({ flow, first }: { flow: Signal<Flow | null>; first: boolean }) {
  const open = useSignal(first), label = useSignal(""), note = useSignal(""), chat = useSignal(false), busy = useSignal(false);
  const clean = label.value.trim().toLowerCase();  // the server checks the rest and says what's wrong
  async function add(e: Event) {
    e.preventDefault();
    if (!clean || busy.value) return;
    busy.value = true;
    const tab = blankTab();  // during the click: the sign-in follows straight on
    const r = await act<Account[]>("account_add", { label: clean, note: note.value, chat: chat.value });
    busy.value = false;
    if (!r) { tab?.close(); return; }
    accounts.value = r;
    label.value = ""; note.value = ""; chat.value = false; open.value = false;
    await startSignIn(flow, clean, 0, tab);
  }
  if (!open.value) return <button class="btn sm gadd" onClick={() => { open.value = true; }}>+ Add a Google account</button>;
  return (
    <form class="srv gaddform" onSubmit={add}>
      <h3>Add a Google account</h3>
      <label class="glabel">Label <span class="sub">lowercase, e.g. work or personal</span>
        <input class="field" autocomplete="off" autocapitalize="none" spellcheck={false} placeholder="work" value={label.value}
          onInput={(e) => { label.value = e.currentTarget.value; }} />
      </label>
      <label class="glabel">What it's for <span class="sub">optional; tells JARVIS when to use it</span>
        <input class="field" autocomplete="off" placeholder="for work" value={note.value} onInput={(e) => { note.value = e.currentTarget.value; }} />
      </label>
      <div class="gchat">
        <button type="button" class="switch" role="switch" aria-checked={chat.value} aria-label="Google Chat" onClick={() => { chat.value = !chat.value; }} />
        <span>Google Workspace account with <b>Google Chat</b></span>
      </div>
      <div class="inline">
        <button class="btn primary" disabled={!clean || busy.value}>Add and sign in</button>
        {!first && <button type="button" class="btn" onClick={() => { open.value = false; }}>Cancel</button>}
      </div>
    </form>
  );
}

export function GoogleAccountsPage() {
  const all = accounts.value, setup = googleSetup.value;
  const flow = useSignal<Flow | null>(null);
  if (!all || !setup) return <Empty>Loading…</Empty>;
  return (
    <>
      {!setup.kind ? <SetupCard setup={setup} /> : <ClientLine setup={setup} />}
      {all.length > 0 && <ul class="list">{all.map((a) => <AccountRow key={a.label} a={a} flow={flow} ready={!!setup.kind} />)}</ul>}
      {setup.kind && <AddAccount flow={flow} first={!all.length} />}
      {!setup.kind && all.length > 0 && <p class="hint">Sign these in once the client is saved.</p>}
      <p class="hint">JARVIS uses a new or changed account from the next conversation; no restart needed.</p>
    </>
  );
}

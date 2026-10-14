// Claude logins: the Claude accounts JARVIS can run on, by priority (jarvis.core.claude_logins). JARVIS runs on the
// first one that's usable, moving down the list while one's limit is used up or its sign-in stopped working.
// Signing in runs Claude Code's own sign-in on JARVIS's computer (jarvis.claude_login): Claude's page opens in a new
// tab, on any device, and the code it shows at the end is pasted here. Only the owner's login changes them.
import { useSignal, type Signal } from "@preact/signals";
import { act, ago, claudeLogins, toast, type ClaudeLogin, type ClaudeLogins, type ClaudeResult, type ClaudeSignIn } from "../../state/panel";
import { now } from "../../state/store";
import { DownIcon, TrashIcon, UpIcon } from "../icons";
import { Empty, Led, Pill } from "./bits";
import { blankTab } from "./GoogleAccounts";

const called = (l: Pick<ClaudeLogin, "name">) => l.name || "default";
const broken = (l: ClaudeLogin) => !l.signed_in || l.check?.ok === false;

export function claudeSummary(): [string, "ok" | "warn" | null] {
  const c = claudeLogins.value;
  if (!c) return ["The Claude accounts JARVIS runs on", null];
  const on = c.logins.find((l) => l.active);
  if (!on) return ["The Claude accounts JARVIS runs on", null];
  if (broken(on)) return [`The ${called(on)} login needs signing in again`, "warn"];
  const first = c.logins[0], more = c.logins.length > 1 ? ` · ${c.logins.length} logins` : "";
  if (first && !first.active) return [`On ${called(on)} for now: ${called(first)} ${first.skipped.replace(/^its /, "has its ")}`, "warn"];
  return [`Runs on ${on.email ?? `the ${called(on)} login`}${more}`, on.check?.ok ? "ok" : null];
}

// One sign-in: `started` once Claude's link is back, `failed` when the code didn't work
interface Flow { name: string; email: string; started?: ClaudeSignIn; failed?: string }

// `tab` is opened during the click: browsers block tabs opened later
async function startSignIn(flow: Signal<Flow | null>, name: string, email = "", tab: Window | null = blankTab()) {
  flow.value = { name, email };
  const r = await act<ClaudeSignIn>("claude_login_start", { name, email });
  if (!r) { tab?.close(); flow.value = null; return; }
  if (tab && !tab.closed) tab.location.href = r.url;
  flow.value = { name, email, started: r };
}

function SignInBox({ flow }: { flow: Signal<Flow | null> }) {
  const code = useSignal(""), busy = useSignal(false);
  const f = flow.value!;
  const who = f.name ? <>the <b>{f.name}</b> login</> : <>the default login</>;
  async function finish(e: Event) {
    e.preventDefault();
    if (!code.value.trim() || busy.value || !f.started) return;
    busy.value = true;
    const r = await act<ClaudeResult>("claude_login_finish", { state: f.started.state, code: code.value });
    busy.value = false;
    code.value = "";
    if (!r) { flow.value = null; return; }  // the sign-in was over: the toast says so
    claudeLogins.value = r;
    if (r.ok) { toast(r.said); flow.value = null; } else flow.value = { ...f, failed: r.said };
  }
  function cancel() {
    if (f.started && !f.failed) act("claude_login_cancel", { state: f.started.state });
    flow.value = null;
  }
  if (!f.started) return <div class="note gsign">Starting Claude's sign-in…</div>;
  if (f.failed) {
    return (
      <div class="note amber gsign">
        <div class="grow">{f.failed}</div>
        <div class="inline wrap">
          <button class="btn sm primary" onClick={() => startSignIn(flow, f.name, f.email)}>Try again</button>
          <button class="btn sm" onClick={cancel}>Close</button>
        </div>
      </div>
    );
  }
  return (
    <div class="note gsign">
      <div class="grow">
        Sign in to Claude in the new tab for {who}, with the account JARVIS should use.
        {" "}<a href={f.started.url} target="_blank" rel="noopener">Open Claude's sign-in</a> if no tab opened.
      </div>
      <div class="sub">At the end Claude shows a code: copy it and paste it here.</div>
      <form class="inline wrap" onSubmit={finish}>
        <input class="field mono grow" autocomplete="off" spellcheck={false} placeholder="Code from Claude's page"
          aria-label="Code from Claude's page" value={code.value} onInput={(e) => { code.value = e.currentTarget.value; }} />
        <button class="btn sm primary" disabled={busy.value || !code.value.trim()}>{busy.value ? "Signing in and checking…" : "Finish"}</button>
        <button type="button" class="btn sm" disabled={busy.value} onClick={cancel}>Cancel</button>
      </form>
    </div>
  );
}

// Move a login up (-1) or down (+1) the priority order; shown moved at once, put back if the server refuses
async function move(at: number, by: -1 | 1) {
  const c = claudeLogins.peek();
  if (!c || at + by < 0 || at + by >= c.logins.length) return;
  const moved = [...c.logins];
  moved.splice(at + by, 0, ...moved.splice(at, 1));
  claudeLogins.value = { ...c, logins: moved };
  const r = await act<ClaudeLogins>("claude_login_order", { names: moved.map((l) => l.name) });
  claudeLogins.value = r ?? c;
}

function LoginRow({ l, at, count, flow, can }: { l: ClaudeLogin; at: number; count: number; flow: Signal<Flow | null>; can: boolean }) {
  const busy = useSignal("");
  async function run<T extends ClaudeLogins>(action: string, done?: string): Promise<T | null> {
    busy.value = action;
    const r = await act<T>(action, { name: l.name }, done);
    busy.value = "";
    if (r) claudeLogins.value = r;
    return r;
  }
  async function check() {
    const r = await run<ClaudeResult>("claude_login_check");
    if (r) toast(r.said);
  }
  async function remove() {
    if (confirm(`Sign the ${l.name} login out and remove it?${l.active ? " JARVIS goes back to the default login." : ""}`)) {
      await run("claude_login_remove", "Removed");
    }
  }
  const led = !l.signed_in ? "bad" : l.skipped ? "warn" : !l.check ? "off" : l.check.ok ? "ok" : "bad";
  const account = [l.email, l.org, l.plan && `${l.plan} plan`].filter(Boolean).join(" · ");
  return (
    <li class="row">
      <Led kind={led} />
      <div class="grow">
        <div><span class="sub">{at + 1}.</span> <b>{l.name || "Default"}</b> {l.active && <Pill kind="running">in use</Pill>}</div>
        <div class="sub">{l.signed_in ? account || "Signed in" : "Not signed in"}{!l.name && " · this computer's Claude Code sign-in"}</div>
        {l.skipped && <div class="sub warn">Skipped: {l.skipped}.</div>}
        {l.check && <div class="sub">{l.check.said} Checked {ago(l.check.at, now.value)}.</div>}
        {l.usage && <div class="sub">{l.usage}</div>}
        {can && (
          <div class="inline wrap">
            <button class="btn sm" disabled={!!busy.value || !!flow.value} onClick={() => startSignIn(flow, l.name)}>
              {l.signed_in ? "Sign in again" : "Sign in"}</button>
            {l.signed_in && <button class="btn sm" disabled={!!busy.value} onClick={check}>{busy.value === "claude_login_check" ? "Checking…" : "Check"}</button>}
          </div>
        )}
      </div>
      {can && (
        <span class="rt">
          <button class="ico" title="Higher priority" aria-label="Move up" disabled={at === 0} onClick={() => move(at, -1)}><UpIcon /></button>
          <button class="ico" title="Lower priority" aria-label="Move down" disabled={at === count - 1} onClick={() => move(at, 1)}><DownIcon /></button>
          {l.name && <button class="ico" title="Remove" aria-label="Remove" disabled={!!busy.value} onClick={remove}><TrashIcon /></button>}
        </span>
      )}
    </li>
  );
}

function AddForm({ flow }: { flow: Signal<Flow | null> }) {
  const name = useSignal(""), email = useSignal("");
  const valid = /^[a-z0-9][a-z0-9-]{0,31}$/.test(name.value) && !["default", "order"].includes(name.value);
  function add(e: Event) {
    e.preventDefault();
    if (!valid || flow.value) return;
    startSignIn(flow, name.value, email.value.trim());  // its tab opens now, during the click
    name.value = email.value = "";
  }
  return (
    <form class="inline wrap" onSubmit={add}>
      <input class="field" placeholder="Name, e.g. work" aria-label="Name" autocomplete="off" value={name.value}
        onInput={(e) => { name.value = e.currentTarget.value.toLowerCase().replace(/[^a-z0-9-]/g, ""); }} />
      <input class="field" type="email" placeholder="Email (optional)" aria-label="Email" autocomplete="off" value={email.value}
        onInput={(e) => { email.value = e.currentTarget.value; }} />
      <button class="btn sm primary" disabled={!valid || !!flow.value}>Add and sign in</button>
    </form>
  );
}

export function ClaudeLoginsPage() {
  const flow = useSignal<Flow | null>(null);
  const c = claudeLogins.value;
  if (!c) return <Empty>Loading…</Empty>;
  return (
    <>
      {flow.value && <SignInBox flow={flow} />}
      <ul class="list">{c.logins.map((l, i) => <LoginRow key={l.name} l={l} at={i} count={c.logins.length} flow={flow} can={c.can_change} />)}</ul>
      {c.can_change ? (
        <>
          <div class="grp">Add a login</div>
          <AddForm flow={flow} />
        </>
      ) : <p class="hint">Only the owner's login can sign these in or change which one JARVIS runs on.</p>}
      <p class="hint">JARVIS runs on the first login in this order that it can use. It moves down the list while a
        login's limit is used up or its sign-in stopped working, and back up once it resets or is signed in again; a
        task already started carries on where it can. Each login is a Claude account with limits of its own. Signing in
        here saves the login where JARVIS's services read it, so it also fixes one that stopped working after signing
        in from a remote terminal.</p>
    </>
  );
}

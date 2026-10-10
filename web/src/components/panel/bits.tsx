// Shared panel components.
import type { ComponentChildren } from "preact";
import { useSignal } from "@preact/signals";

const STATUS_PILL: Record<string, string> = {
  queued: "running", running: "running", waiting: "waiting", done: "done", failed: "failed", stopped: "muted", discarded: "muted",
};

export const Pill = ({ kind, children }: { kind: string; children: ComponentChildren }) => <span class={`tag ${kind}`}>{children}</span>;

export const Led = ({ kind }: { kind: "ok" | "warn" | "bad" | "run" | "off" }) => <span class={`dot ${kind}`} />;

export function StatusPill({ status, requestsOnly }: { status: string; requestsOnly?: boolean }) {
  const text = status === "waiting" ? (requestsOnly ? "needs a yes" : "question") : status;
  return <Pill kind={STATUS_PILL[status] ?? "muted"}>{text}</Pill>;
}

// One-line form: field and button, cleared when `send` succeeds.
export function InlineForm({ placeholder, label, send, initial = "", primary = false, children }: {
  placeholder: string; label: string; send: (text: string) => Promise<boolean>; initial?: string; primary?: boolean;
  children?: ComponentChildren;
}) {
  const text = useSignal(initial), busy = useSignal(false);
  async function submit(e: Event) {
    e.preventDefault();
    const t = text.value.trim();
    if (!t || busy.value) return;
    busy.value = true;
    if (await send(t)) text.value = "";
    busy.value = false;
  }
  return (
    <form class="inline" onSubmit={submit}>
      <input class="field" placeholder={placeholder} aria-label={placeholder} value={text.value}
        onInput={(e) => { text.value = e.currentTarget.value; }} />
      <button class={`btn sm${primary ? " primary" : ""}`} disabled={busy.value || !text.value.trim()} aria-label={label}>{children ?? label}</button>
    </form>
  );
}

export const Empty = ({ children }: { children: ComponentChildren }) => <p class="empty">{children}</p>;

export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

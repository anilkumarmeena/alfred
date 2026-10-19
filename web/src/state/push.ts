// Push notifications: Web Push (iOS 16.4+ Home Screen app or any supporting browser), or APNs in the iOS app.
// public/sw.js shows them; a tap opens the page on the task or alert (showFocus).
import { postAuthed, postAuthedJson, Unauthorized } from "../lib/api";
import { DEVICE, iOS, standalone } from "../lib/device";
import { appNotifications, nativeAudio, nativePlatform, type AppNotifications } from "../lib/native";
import { PUSH_KEY, storage } from "../lib/storage";
import { openTask, toast, visible, type ToastAction } from "./panel";
import { appNotify, assistantName, focus, passcode, pushKey, pushLater, pushSub, setPasscode, showError, showLogin, showNotice } from "./store";

// pushKey: server VAPID public key (base64url); empty when push is off server-side.
const push = {
  reg: null as ServiceWorkerRegistration | null,
  why: "",  // shown when there's no key; empty: couldn't reach the server
};

// iOS app: no Web Push in the web view, so the plugin registers with APNs.
const app: AppNotifications | null = (() => {
  const plugin = nativeAudio();
  return plugin && nativePlatform() === "ios" ? appNotifications(plugin) : null;
})();

// Toast when the panel is visible or there's an action, else the stage notice.
function say(msg: string, action?: ToastAction) {
  if (action || visible.peek()) toast(msg, action);
  else showNotice(msg);
}

function pushBlocked(): string {
  // Android app: it asks for permission and registers with FCM itself at session start, so there's nothing to call
  if (nativeAudio()) return "The app turns Alfred's notifications on by itself when a session starts. If none arrive, allow notifications for Alfred in Android's settings.";
  if (iOS && !standalone) return "To get notifications on iPhone, add Alfred to your Home Screen first: tap Share, then Add to Home Screen, and open Alfred from the new icon.";
  if (!window.isSecureContext) return `Notifications need ${assistantName.peek()}'s HTTPS address (Tailscale Serve), not plain http.`;
  if (!("serviceWorker" in navigator && "PushManager" in window && "Notification" in window)) return "This browser can't show push notifications. On iPhone they need iOS 16.4 or later, in the Home Screen app.";
  return "";
}

function putOff(later: boolean) {  // later: user deferred, so the pill stays hidden
  storage.set(PUSH_KEY, later ? "later" : null);
  pushLater.value = later;
}

function keyBytes(b64: string): Uint8Array<ArrayBuffer> {  // base64url -> bytes
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - b64.length % 4) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
function sameKey(buf: ArrayBuffer | null | undefined, b64: string): boolean {
  if (!buf) return true;  // unknown: assume ours
  const a = new Uint8Array(buf), b = keyBytes(b64);
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function savePush(sub: PushSubscription) {
  return postAuthedJson("/api/push/subscribe", { device: DEVICE, subscription: sub.toJSON() });
}

// ── iOS app ───────────────────────────────────────────────────

const openSettings: ToastAction = { label: "Open Settings", run: () => { app?.openSettings(); } };

export async function refreshAppPush(): Promise<void> {
  if (app) appNotify.value = await app.state();
}

async function enableAppPush(app: AppNotifications): Promise<void> {
  const before = await app.state();
  appNotify.value = before;
  // iOS prompts only once; after a refusal only Settings can enable them.
  if (before === "denied") return say("Notifications are blocked for Alfred. Allow them in iOS Settings, under Notifications.", openSettings);
  if (!passcode.peek()) return showLogin("Enter your passcode, then turn notifications on again.");
  try {
    const r = await app.request(passcode.peek()!);
    appNotify.value = r.state;
    if (r.state === "denied") return say("Notifications weren't allowed. You can turn them on in iOS Settings any time.", openSettings);
    if (r.state !== "granted") return say("Notifications weren't allowed. Try again when you're ready.");
    say(r.registered || before === "granted" ? `Notifications are on for this iPhone. Tap one to open ${assistantName.peek()} on that task or alert.`
      : `Notifications are on. This iPhone signs up with ${assistantName.peek()} when your next session starts.`);
  } catch (e: any) {
    if (e?.code === "UNAUTHORIZED") { setPasscode(null); return showLogin(e.message || "Wrong passcode."); }
    say(e?.message || String(e));
  }
}

function disableAppPush(): void {
  // Apps can't revoke their own permission.
  say("iOS keeps the switch for Alfred's notifications: turn them off in Settings, under Notifications.", openSettings);
}

/** Open Alfred's page in iOS Settings (iOS app only). */
export function openAppSettings() { app?.openSettings(); }

// ── Browser ─────────────────────────────────────────────────────

export async function enablePush(): Promise<void> {
  if (app) return enableAppPush(app);
  const why = pushBlocked();
  if (why) { putOff(true); return say(why); }
  if (!pushKey.peek()) return say(push.why || `Couldn't reach ${assistantName.peek()}.`);
  if (!passcode.peek()) return showLogin("Enter your passcode, then turn notifications on again.");
  showError("");
  try {
    // Ask first, while still inside the user gesture: iOS only prompts from one.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      putOff(true);
      return say(permission === "denied"
        ? "Notifications are blocked for Alfred. Allow them in Settings, under Notifications, then try again."
        : "Notifications weren't allowed. Try again when you're ready.");
    }
    const reg = push.reg || (push.reg = await navigator.serviceWorker.register("/sw.js"));
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    const key = pushKey.peek();
    if (sub && !sameKey(sub.options?.applicationServerKey, key)) { await sub.unsubscribe(); sub = null; }
    sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    await savePush(sub);
    pushSub.value = sub; putOff(false);
    say(`Notifications are on. Tap one to open ${assistantName.peek()} on that task or alert.`);
  } catch (e) {
    if (e instanceof Unauthorized) { setPasscode(null); return showLogin(e.message); }
    say((e as Error).message || String(e));
  }
}

async function disablePush(): Promise<void> {
  if (!confirm("Turn off Alfred notifications on this device?")) return;
  const sub = pushSub.peek()!;
  pushSub.value = null;
  try {
    await postAuthed("/api/push/unsubscribe", { endpoint: sub.endpoint });
    await sub.unsubscribe();
  } catch {}
  say("Notifications are off on this device.");
}

export function toggleNotifications() {
  if (app) return appNotify.peek() === "granted" ? disableAppPush() : enableAppPush(app);
  return pushSub.peek() ? disablePush() : enablePush();
}

const AUDIO = /\.(m4a|mp3|wav|aac)$/i;

// Show the focus card for a tapped notification: /?task=3, /?alert=calendar or /?file=TOKEN&name=…
function showFocus({ url, title, body }: { url: string; title?: string | null; body?: string | null }) {
  const q = new URL(url, location.origin).searchParams, task = q.get("task"), file = q.get("file");
  if (file && /^[\w-]{32,64}$/.test(file)) {
    const name = q.get("name") || "file", who = assistantName.peek();
    focus.value = {
      label: title || `From ${who}`, body: body || name, talk: `Talk to ${who} about it`,
      file: { src: `/api/files/${file}`, name, audio: AUDIO.test(name) },
    };
    return;
  }
  if (!task && !q.get("alert")) return;
  if (task && passcode.peek()) openTask(Number(task));
  focus.value = {
    label: task ? `Task ${task}` : title || "Heads up",
    body: body || "",
    talk: task ? `Talk to ${assistantName.peek()} about task ${task}` : `Talk to ${assistantName.peek()} about it`,
  };
}

// Page opened from a notification: /?task=3#title=…&body=… (or ?alert=…, ?file=…)
export function openedFromNotification() {
  if (!location.search) return;
  const h = new URLSearchParams(location.hash.slice(1));
  showFocus({ url: location.href, title: h.get("title"), body: h.get("body") });
  history.replaceState(null, "", "/");
}

export async function setupPush(): Promise<void> {
  if (app) {  // reflect iOS permission, rechecked on return from Settings
    await refreshAppPush();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAppPush(); });
    return;
  }
  if (nativeAudio()) return;  // no Web Push in the app's web view
  if ("serviceWorker" in navigator && window.isSecureContext) {
    // Page already open: the service worker forwards the tap without a reload.
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data?.type === "notification") showFocus(e.data); });
    try { push.reg = await navigator.serviceWorker.register("/sw.js"); } catch {}
  }
  try {
    const d = await (await fetch("/api/push/key", { cache: "no-store" })).json();
    pushKey.value = (d.enabled && d.key) || ""; push.why = d.error || push.why;
  } catch {}
  try {
    if (push.reg && "PushManager" in window && Notification.permission === "granted") pushSub.value = await push.reg.pushManager.getSubscription();
  } catch {}
  // Re-register in case the server dropped this device or lost its data.
  const sub = pushSub.peek();
  if (sub && pushKey.peek() && passcode.peek()) savePush(sub).catch(() => {});
}

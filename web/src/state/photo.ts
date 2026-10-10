// Photo attached in the message box. Downscaled and uploaded at once to /api/picture, where it waits for the next
// turn (typed or spoken). The first user line after upload consumes it and the box clears.
import { effect, signal } from "@preact/signals";
import { postAuthedJson, Unauthorized } from "../lib/api";
import { DEVICE } from "../lib/device";
import { base64, shrink } from "../lib/photo";
import { passcode, setPasscode, showLogin } from "./store";
import { lines } from "./transcript";

export interface Photo {
  preview: string;  // object URL for the thumbnail
  state: "sending" | "ready" | "failed";
  id?: string;  // server id, once uploaded
  error?: string;
  after: number;  // last transcript line before upload; a later user line consumes the photo
}

export const ASK = "What's in this photo?";  // default text for a photo with no words
export const photo = signal<Photo | null>(null);
let sending: Promise<boolean> = Promise.resolve(false);

const lastLine = () => lines.peek()[lines.peek().length - 1]?.id ?? -1;

function clear() {
  const p = photo.peek();
  if (p) URL.revokeObjectURL(p.preview);
  photo.value = null;
}

function drop(id: string) {
  postAuthedJson("/api/picture/drop", { id }).catch(() => {});  // best effort: it expires server-side anyway
}

// Attach a photo (replacing any previous one) and start uploading.
export function attach(file: File): void {
  if (!passcode.peek()) return showLogin();
  discard();
  const p: Photo = { preview: URL.createObjectURL(file), state: "sending", after: -1 };
  photo.value = p;
  const current = () => photo.peek()?.preview === p.preview;
  sending = (async () => {
    try {
      const data = await base64(await shrink(file));
      const { id } = await postAuthedJson("/api/picture", { device: DEVICE, data });
      if (!current()) { drop(id); return false; }  // removed or replaced meanwhile
      photo.value = { ...p, state: "ready", id, after: lastLine() };
      return true;
    } catch (e) {
      if (e instanceof Unauthorized) { setPasscode(null); showLogin(e.message); }
      if (current()) photo.value = { ...p, state: "failed", error: (e as Error).message || String(e) };
      return false;
    }
  })();
}

// Resolves to whether the latest photo uploaded.
export const photoReady = (): Promise<boolean> => sending;

// User removed the photo before using it.
export function discard(): void {
  const p = photo.peek();
  if (p?.state === "ready" && p.id) drop(p.id);
  clear();
}

// A user turn after upload consumed the photo.
effect(() => {
  const p = photo.value, all = lines.value;
  if (p?.state === "ready" && all.some((l) => l.kind === "msg" && l.who === "you" && l.id > p.after)) clear();
});

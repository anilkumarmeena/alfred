// A photo to show JARVIS, picked in the message box. It's made smaller here and sent to the Mac at once
// (jarvis.server's /api/picture), where it waits for the next turn, typed or spoken (jarvis.state.pictures). Once the
// user says or types something after it's there, that turn has taken it, and the message box lets it go.
import { effect, signal } from "@preact/signals";
import { postAuthedJson, Unauthorized } from "../lib/api";
import { DEVICE } from "../lib/device";
import { base64, shrink } from "../lib/photo";
import { passcode, setPasscode, showLogin } from "./store";
import { lines } from "./transcript";

export interface Photo {
  preview: string;  // an object URL of the file picked, for the thumbnail
  state: "sending" | "ready" | "failed";
  id?: string;  // the Mac's, once it has it
  error?: string;
  after: number;  // the last transcript line before it was ready: a line of the user's after this took it
}

export const ASK = "What's in this photo?";  // sent with a photo and no words
export const photo = signal<Photo | null>(null);
let sending: Promise<boolean> = Promise.resolve(false);

const lastLine = () => lines.peek()[lines.peek().length - 1]?.id ?? -1;

function clear() {
  const p = photo.peek();
  if (p) URL.revokeObjectURL(p.preview);
  photo.value = null;
}

function drop(id: string) {
  postAuthedJson("/api/picture/drop", { id }).catch(() => {});  // left there, it lapses on the Mac by itself
}

// Pick a photo: it replaces one picked before, and starts on its way to the Mac.
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

// Whether the photo picked last reached the Mac, once it has or hasn't.
export const photoReady = (): Promise<boolean> => sending;

// The user took it back before asking about it.
export function discard(): void {
  const p = photo.peek();
  if (p?.state === "ready" && p.id) drop(p.id);
  clear();
}

// Something the user said or typed once it was there went with it: the photo's been seen.
effect(() => {
  const p = photo.value, all = lines.value;
  if (p?.state === "ready" && all.some((l) => l.kind === "msg" && l.who === "you" && l.id > p.after)) clear();
});

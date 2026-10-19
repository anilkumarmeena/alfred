// localStorage wrapper; reads return null where storage is refused (private mode, locked-down browsers).
export const storage = {
  get(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key: string, value: string | null): void {
    try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {}
  },
};

export const PASSCODE_KEY = "alfred.passcode";
export const SPEAKER_KEY = "alfred.speaker";  // "off": text-only replies
export const PUSH_KEY = "alfred.push";  // "later": user deferred notifications; pill hidden
export const SIDE_KEY = "alfred.side";  // "on": desktop opens on a section page, not Home
export const ACTIVITY_KEY = "alfred.activity";  // newest bell entry seen on this device
export const ASSISTANT_KEY = "alfred.assistant";  // the name the user gave the assistant, last heard from the server

// Keys from before the rename (jarvis.*), copied once so a device stays signed in and keeps its choices
for (const key of [PASSCODE_KEY, SPEAKER_KEY, PUSH_KEY, SIDE_KEY, ACTIVITY_KEY]) {
  const old = storage.get(key.replace(/^alfred\./, "jarvis."));
  if (old != null && storage.get(key) == null) storage.set(key, old);
}

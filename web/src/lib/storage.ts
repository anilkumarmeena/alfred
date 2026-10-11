// localStorage wrapper; reads return null where storage is refused (private mode, locked-down browsers).
export const storage = {
  get(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key: string, value: string | null): void {
    try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {}
  },
};

export const PASSCODE_KEY = "jarvis.passcode";
export const SPEAKER_KEY = "jarvis.speaker";  // "off": text-only replies
export const PUSH_KEY = "jarvis.push";  // "later": user deferred notifications; pill hidden
export const SIDE_KEY = "jarvis.side";  // "off": desktop panel column hidden
export const ACTIVITY_KEY = "jarvis.activity";  // newest bell entry seen on this device

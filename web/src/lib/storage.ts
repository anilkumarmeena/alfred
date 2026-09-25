// localStorage, which private browsing or a locked-down browser can refuse: reads then give null.
export const storage = {
  get(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key: string, value: string | null): void {
    try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {}
  },
};

export const PASSCODE_KEY = "jarvis.passcode";
export const CAPTIONS_KEY = "jarvis.captions";  // "off" hides the transcript
export const SPEAKER_KEY = "jarvis.speaker";  // "off": JARVIS replies in text only, from the next session's first word
export const PUSH_KEY = "jarvis.push";  // "later": the user put off notifications, so the pill stays hidden
export const SIDE_KEY = "jarvis.side";  // "off": the desktop's workspace column is hidden
export const ACTIVITY_KEY = "jarvis.activity";  // the newest bell entry this device has shown

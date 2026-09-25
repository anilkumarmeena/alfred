// Keeps the phone's screen on during a session. The browser drops the lock when the page hides, so
// it's asked for again on return.
let wake: WakeLockSentinel | null = null;

export async function keepAwake(on: boolean): Promise<void> {
  try {
    if (on && !wake && "wakeLock" in navigator && document.visibilityState === "visible") {
      wake = await navigator.wakeLock.request("screen");
      wake.addEventListener("release", () => { wake = null; });
    } else if (!on && wake) { await wake.release(); wake = null; }
  } catch {}
}
